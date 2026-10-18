"""«Второй компьютер»: голосом управлять другим ПК в сети, где тоже запущен Jarvis с пультом по Wi-Fi.

В настройках — список компьютеров {name, url (http://192.168.1.20:8766), token (пароль пульта)}. Пароль
хранится в секретах (ctx.secrets, имя second_pc.<id>), а не в настройках; без него берётся общий
remote_token. Раз в 15 с плагин опрашивает компьютеры (GET /remote/api/state): в сети ли, громкость, что
играет. Фразы «на втором компьютере {text}», «на ноутбуке {text}», «на компьютере Ноутбук {text}» отправляют
команду туда (POST /remote/api/dispatch {text}, заголовок X-Remote-Token).
"""

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx

from jarvis import text as textops
from jarvis.bus import Event
from jarvis.db import new_id
from jarvis.plugins import ru
from jarvis.plugins.base import ConfigField, Plugin, PluginAction, error_text, spoken_name
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

PING_S = 15.0
AUTH_BACKOFF_S = 600.0  # пульт блокирует адрес после 20 неверных паролей за 10 минут — не долбим его
DEFAULT_PORT = 8766
SECRET_PREFIX = "second_pc."
TOKEN_HEADER = "X-Remote-Token"


def normalize_url(url: str) -> str:
    """«192.168.1.20» → «http://192.168.1.20:8766»; путь и хвостовой «/» отбрасываются."""
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        parsed = httpx.URL(url)
    except Exception:
        raise UserFacingError(f"Неверный адрес компьютера: {url}") from None
    if not parsed.host:
        raise UserFacingError(f"Неверный адрес компьютера: {url}")
    port = parsed.port or DEFAULT_PORT
    host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    return f"{parsed.scheme}://{host}:{port}"


def _media(data: dict[str, Any]) -> dict[str, Any] | None:
    media = data.get("media") or data.get("now_playing")
    if not isinstance(media, dict):
        return None
    return {k: media.get(k) for k in ("title", "artist", "app", "status") if media.get(k) is not None}


def _volume(data: dict[str, Any]) -> int | None:
    for source in (data, data.get("system") or {}, data.get("audio") or {}):
        if isinstance(source, dict) and isinstance(source.get("volume"), int | float):
            return int(source["volume"])
    return None


class SecondPc(Plugin):
    id = "second_pc"
    name = "Второй компьютер"
    description = (
        "Голосом управляйте другим компьютером в сети, где тоже запущен Jarvis: «на ноутбуке включи музыку»."
    )
    icon = "computer"
    color = ("#22D3EE", "#0891B2")
    fields = (
        ConfigField(
            "pcs",
            "list",
            "Компьютеры",
            default=[],
            hint="На втором компьютере включите в Jarvis «Пульт по Wi-Fi» и задайте пароль пульта",
            fields=(
                ConfigField("name", "text", "Название", default="", placeholder="Ноутбук"),
                ConfigField(
                    "url", "text", "Адрес пульта", default="", placeholder="http://192.168.1.20:8766"
                ),
                ConfigField(
                    "token", "secret", "Пароль пульта", hint="Пусто — общий пароль пульта из настроек"
                ),
            ),
        ),
    )
    actions = (PluginAction("ping", "Проверить связь"),)
    examples = ("на ноутбуке включи музыку", "на втором компьютере сверни все окна")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        self._states: dict[str, dict[str, Any]] = {}
        self._wake: asyncio.Event | None = None
        self._published: Any = None
        self._auth_backoff: dict[str, float] = {}  # id → до какого time.monotonic() не опрашивать
        self._unsubscribe: Any = None

    # --- настройки ------------------------------------------------------------------------------------
    def pcs(self) -> list[dict[str, Any]]:
        return [pc for pc in self.config.get("pcs") or [] if pc.get("id") and pc.get("url")]

    def prepare_config(self, config: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
        """Пароли — из элементов списка в секреты; каждому компьютеру — постоянный id."""
        pcs: list[dict[str, Any]] = []
        tokens: dict[str, str] = {}  # id → новый пароль ("" — удалить)
        for raw in config.get("pcs") or []:
            item = dict(raw)
            token = item.pop("token", "")
            name = " ".join(str(item.get("name") or "").split())
            url = normalize_url(str(item.get("url") or ""))
            if not url:
                raise UserFacingError(f"У компьютера «{name or 'без названия'}» не указан адрес")
            pc_id = str(item.get("id") or "") or new_id()
            while pc_id in tokens or any(pc["id"] == pc_id for pc in pcs):
                pc_id = new_id()
            if token is None:
                tokens[pc_id] = ""
            elif token:
                tokens[pc_id] = str(token)
            pcs.append({"id": pc_id, "name": name or f"Компьютер {len(pcs) + 1}", "url": url})
        # всё проверено — теперь секреты: новые пароли и удалённые компьютеры
        ids = {pc["id"] for pc in pcs}
        for pc in old.get("pcs") or []:
            old_id = str(pc.get("id") or "")
            if old_id and old_id not in ids:
                tokens[old_id] = ""
        for pc_id, token in tokens.items():
            if self.secret(SECRET_PREFIX + pc_id) != token:
                self.ctx.secrets.set(SECRET_PREFIX + pc_id, token)
        return {**config, "pcs": pcs}

    def public_config(self) -> dict[str, Any]:
        config = self.config
        config["pcs"] = [
            {**pc, "token": "", "token_set": bool(self.secret(SECRET_PREFIX + str(pc.get("id"))))}
            for pc in config.get("pcs") or []
        ]
        return config

    def _token(self, pc: dict[str, Any]) -> str:
        return self.secret(SECRET_PREFIX + str(pc.get("id"))) or self.secret("remote_token")

    def _headers(self, pc: dict[str, Any]) -> dict[str, str]:
        token = self._token(pc)
        return {TOKEN_HEADER: token} if token else {}

    def find(self, args: dict[str, Any]) -> dict[str, Any]:
        pcs = self.pcs()
        if not pcs:
            raise UserFacingError("Компьютеры не добавлены — укажите их в настройках плагина")
        pc_id = str(args.get("id") or "")
        name = textops.normalize(str(args.get("name") or ""))
        for pc in pcs:
            if (pc_id and pc["id"] == pc_id) or (name and textops.normalize(pc["name"]) == name):
                return pc
        if pc_id or name:
            raise UserFacingError("Такой компьютер не найден")
        return pcs[0]

    # --- жизненный цикл ------------------------------------------------------------------------------
    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._unsubscribe = self.ctx.bus.on("secrets.changed", self._on_secret)
        self.spawn(self._loop(), "ping")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._states = {}
        self._published = None
        self._auth_backoff = {}

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        known = {pc["id"] for pc in self.pcs()}
        self._states = {k: v for k, v in self._states.items() if k in known}
        self._auth_backoff = {}
        if self._wake is not None:
            self._wake.set()

    def _on_secret(self, event: Event) -> None:
        name = str(event.data.get("name") or "")
        if name == "remote_token" or name.startswith(SECRET_PREFIX):
            self._auth_backoff = {}  # пароль поменяли — можно пробовать снова
            if self._wake is not None:
                self._wake.set()

    async def _loop(self) -> None:
        assert self._wake is not None
        while True:
            self._wake.clear()
            try:
                await self.ping_all()
            except Exception:
                log.exception("второй компьютер: опрос упал")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(PING_S):
                    await self._wake.wait()

    # --- связь ----------------------------------------------------------------------------------------
    async def ping(self, pc: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        previous = self._states.get(pc["id"], {})
        if not force and self._auth_backoff.get(pc["id"], 0.0) > time.monotonic():
            return previous
        state: dict[str, Any] = {"online": False, "error": "", "last_seen": previous.get("last_seen", 0.0)}
        started = time.monotonic()
        try:
            async with self.http(timeout=5.0) as client:
                response = await client.get(f"{pc['url']}/remote/api/state", headers=self._headers(pc))
        except httpx.HTTPError as e:
            state["error"] = (
                "Не в сети" if isinstance(e, httpx.ConnectError | httpx.TimeoutException) else error_text(e)
            )
            self._states[pc["id"]] = state
            return state
        state["latency_ms"] = int((time.monotonic() - started) * 1000)
        if response.status_code in (401, 403, 429):
            state["error"] = (
                "Пульт временно не пускает: много неверных паролей"
                if response.status_code == 429
                else "Неверный пароль пульта"
            )
            self._auth_backoff[pc["id"]] = time.monotonic() + AUTH_BACKOFF_S
        elif response.status_code >= 400:
            state["error"] = f"Ошибка {response.status_code}"
        else:
            self._auth_backoff.pop(pc["id"], None)
            state["online"] = True
            state["last_seen"] = time.time()
            try:
                data = response.json()
            except ValueError:
                data = {}
            if isinstance(data, dict):
                state["volume"] = _volume(data)
                state["muted"] = bool(data.get("muted")) if "muted" in data else None
                state["now_playing"] = _media(data)
                for key, out in (
                    ("pc_name", "remote_name"),
                    ("version", "version"),
                    ("assistant", "assistant"),
                ):
                    if data.get(key):
                        state[out] = data[key]
        self._states[pc["id"]] = state
        return state

    async def ping_all(self) -> list[dict[str, Any]]:
        pcs = self.pcs()
        if pcs:
            await asyncio.gather(*(self.ping(pc) for pc in pcs))
        self._publish()
        return self._items()

    def _items(self) -> list[dict[str, Any]]:
        items = []
        for pc in self.pcs():
            state = self._states.get(pc["id"], {"online": None, "error": ""})
            items.append({"id": pc["id"], "name": pc["name"], "url": pc["url"], **state})
        return items

    def _publish(self) -> None:
        items = self._items()
        key = [
            (i["id"], i.get("online"), i.get("error"), i.get("volume"), i.get("now_playing")) for i in items
        ]
        if key != self._published:
            online_changed = [k[:3] for k in key] != [k[:3] for k in self._published or []]
            self._published = key
            self.publish("state", {"pcs": items})
            if online_changed:
                self.changed()

    async def dispatch(self, pc: dict[str, Any], text: str) -> ExecResult:
        name = pc["name"]
        try:
            async with self.http(timeout=30.0) as client:
                response = await client.post(
                    f"{pc['url']}/remote/api/dispatch", headers=self._headers(pc), json={"text": text}
                )
        except httpx.TimeoutException:
            raise UserFacingError(f"«{name}» не ответил вовремя") from None
        except httpx.HTTPError:
            self._states[pc["id"]] = {**self._states.get(pc["id"], {}), "online": False, "error": "Не в сети"}
            self._publish()
            raise UserFacingError(f"«{name}» не в сети") from None
        if response.status_code in (401, 403):
            self._auth_backoff[pc["id"]] = time.monotonic() + AUTH_BACKOFF_S
            raise UserFacingError(f"«{name}»: неверный пароль пульта")
        if response.status_code == 429:
            raise UserFacingError(f"«{name}»: пульт временно не пускает — слишком много неверных паролей")
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else ""
            raise UserFacingError(f"«{name}»: {detail or f'ошибка {response.status_code}'}")
        return self._result(pc, text, data if isinstance(data, dict) else {})

    def _result(self, pc: dict[str, Any], text: str, data: dict[str, Any]) -> ExecResult:
        """Ответ пульта ({kind, ok, title, message, text}; полный итог диспетчера тоже понимаем)."""
        name = pc["name"]
        title = f"{name}: {text}"
        extra = {"pc": pc["id"], "remote": data}
        kind = str(data.get("kind") or "")
        if kind == "command":
            nested = data.get("result")
            src = nested if isinstance(nested, dict) else data
            ok = bool(src.get("ok"))
            return ExecResult(
                ok,
                f"{name}: {src.get('title') or text}",
                str(src.get("message") or ("Выполнено" if ok else "Не получилось")),
                icon=str(src.get("icon") or "computer"),
                say=str(src.get("say") or ""),
                data=extra,
            )
        if kind == "ai":
            error = str(data.get("error") or (data.get("message") if data.get("ok") is False else "") or "")
            if error:
                return ExecResult(False, title, error, icon="ai", data=extra)
            answer = " ".join(str(data.get("text") or "").split())
            short = answer if len(answer) <= 140 else answer[:139] + "…"
            return ExecResult(True, title, short or "Готово", icon="ai", say=answer[:400], data=extra)
        if kind == "unknown":
            return ExecResult(False, title, f"На «{name}» такой команды нет", icon="computer", data=extra)
        if kind == "stop":
            return ExecResult(True, title, "Остановлено", icon="computer", data=extra)
        if kind in ("", "empty"):
            return ExecResult(False, title, "Пустая команда", icon="computer", data=extra)
        ok = bool(data.get("ok", True))
        return ExecResult(ok, title, str(data.get("message") or "Выполнено"), icon="computer", data=extra)

    # --- статус и действия ---------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        items = self._items()
        return {"pcs": items, "online": sum(1 for i in items if i.get("online"))}

    async def act_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("id") or args.get("name"):
            pc = self.find(args)
            state = await self.ping(pc, force=True)
            self._publish()
            return {"pc": {"id": pc["id"], "name": pc["name"], **state}}
        self._auth_backoff = {}  # человек сам попросил проверить — пробуем все
        return {"pcs": await self.ping_all()}

    async def act_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        text = textops.clean(str(args.get("text") or ""))
        if not text:
            raise UserFacingError("Не указана команда")
        result = await self.dispatch(self.find(args), text)
        return {"ok": result.ok, "result": result.to_json()}

    def commands(self) -> list[DynamicCommand]:
        out: list[DynamicCommand] = []
        for index, pc in enumerate(self.pcs()):
            name = spoken_name(pc["name"])
            phrases = (
                [f"на компьютере {name} {{text}}", f"на {ru.prepositional(name)} {{text}}"] if name else []
            )
            if index == 0:
                phrases += [
                    "на втором компьютере {text}",
                    "на другом компьютере {text}",
                    "на втором пк {text}",
                ]
            out.append(
                self.command(
                    f"dispatch:{pc['id']}",
                    f"На «{pc['name']}»",
                    phrases,
                    lambda slots, pc_id=pc["id"]: self._cmd_dispatch(pc_id, slots),
                    icon="computer",
                )
            )
        return out

    async def _cmd_dispatch(self, pc_id: str, slots: dict[str, Any]) -> ExecResult:
        pc = self.find({"id": pc_id})
        text = textops.clean(str(slots.get("text") or slots.get("query") or ""))
        if not text:
            return ExecResult(
                False, f"На «{pc['name']}»", "Скажите команду после названия компьютера", icon="computer"
            )
        return await self.dispatch(pc, text)
