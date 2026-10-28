"""Действия фоновой задачи в браузере агента (browser_*): своя вкладка задачи в браузере без окна.

Механика — та же, что у «Браузера Jarvis» экранного агента (BrowserTools: снимок страницы по дереву
доступности, нажатие мышью браузера с проверкой, что элемент не закрыт, ввод через CDP, окна сайта, ожидание
загрузки); отличается остальное:
- окна на экране нет — ничего не проверяется по окну впереди, курсор агента на экране не рисуется;
- вкладка одна — своя у задачи (AgentBrowser.task_tab); вкладками человека задача не пользуется;
- человек взял вкладку в панели («Взять управление») — действие ждёт, пока он не вернёт её, и не выполняется:
  модель сначала смотрит на страницу заново;
- разрешения — вопросом задачи (Background.approve), с учётом «Самостоятельности»
  (settings.background.autonomy): деньги, отправка и публикация от имени человека — всегда, удаление — кроме
  «Спрашивать реже», остальное рискованное (RISKY_NAME) — при «Спрашивать чаще»;
- пароли, коды и данные карт задача не вводит (их вводит человек в панели), файлы не скачивает и не загружает.
"""

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

from jarvis.ai.agent import RISKY_NAME, SEND_NAME, Outcome
from jarvis.ai.browser import page as pages
from jarvis.ai.browser.agent_browser import AgentBrowser
from jarvis.ai.browser.cdp import CdpError
from jarvis.ai.browser.tools import (
    BACK_KEYS,
    CLICK_SETTLE_S,
    CLOSE_TAB_KEYS,
    FORWARD_KEYS,
    LOAD_TIMEOUT_S,
    MESSENGER_HOSTS,
    PAGE_LABEL,
    PASTE_KEYS,
    RELOAD_KEYS,
    BrowserTools,
    _net_error,
    _said,
    _url,
)
from jarvis.ai.format import one_line
from jarvis.ai.policy import has_card_number, normalize_combo
from jarvis.ai.tools import untrusted
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.background.runner import Session
    from jarvis.background.tools import TaskTool

log = logging.getLogger(__name__)

MONEY = re.compile(
    r"\b(pay|buy|order|purchase|checkout|subscribe|donate|transfer)\b|"
    r"оплат|купить|купи\b|покупк|заказ|оформить|перевести|перевод\b|подписаться на тариф|пополнить",
    re.I,
)
DELETE = re.compile(r"\b(delete|remove|erase|wipe)\b|удал|стереть|очистить", re.I)
SECRET_FIELD = (
    "Ошибка: пароли, коды и данные карт задача не вводит — попроси человека (ask_user): он введёт их сам в "
    "панели «Браузер агента» в Jarvis, а ты продолжишь после его ответа."
)
NO_FILES = (
    "Ошибка: скачивать и загружать файлы фоновая задача не может — дай ссылку человеку в итоге или попроси "
    "его (ask_user) сделать это в панели «Браузер агента»."
)
HELD = (
    "Пока человек управлял страницей, ничего не сделано — страница могла измениться: посмотри на неё заново "
    "(browser_read) и продолжай."
)
TASK_BROWSER_RULES = (
    "Браузер агента — твоя вкладка в Chrome без окна (человек видит её в Jarvis и может взять управление). "
    "browser_open — открыть адрес; ответ каждого действия — снимок страницы: адрес, текст и элементы с id "
    "b1, b2… (id не меняются, пока открыта та же страница); browser_click, browser_type — по id; длинная "
    "страница — browser_read по частям; ждать загрузку или текст — browser_wait; назад — browser_press "
    "alt+left."
)


class _Shim:
    """То, что механика BrowserTools берёт у экранного агента: контекст и курсор на экране (его нет)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.ui = None

    async def _point(self, *args: Any, **kwargs: Any) -> None:
        return None


class TaskBrowser(BrowserTools):
    """Браузер одной фоновой задачи на один заход."""

    def __init__(self, browser: AgentBrowser) -> None:
        super().__init__(_Shim(browser.ctx))
        self.agent_browser = browser
        self.unsent: dict[str, str] = {}  # сайт-мессенджер → набранное и не отправленное

    @property
    def browser(self) -> AgentBrowser:
        return self.agent_browser

    @property
    def enabled(self) -> bool:
        return True

    def _browser(self) -> AgentBrowser:
        return self.agent_browser

    def _start(self, run: Any) -> None:
        """Вкладка задачи выбирается в _front (своя на задачу), сбрасывать нечего."""
        return None

    # --- вкладка задачи и человек -------------------------------------------------------------------------
    async def _tab_of(self, run: "Session") -> str:
        target = await self.agent_browser.task_tab(run.task.id)
        if target != self.tab:
            self.tab, self.page = target, None
        run.tab = target
        return target

    async def _front(self, run: "Session") -> tuple[AgentBrowser, None]:
        browser = self.agent_browser
        target = await self._tab_of(run)
        if await browser.wait_free(target, lambda: run.closing):
            self.page = None
            raise UserFacingError(HELD)
        return browser, None

    async def _done(self, run: "Session", result: str, detail: str = "") -> Outcome:
        """Ответ действия — со свежим снимком страницы."""
        browser = self.agent_browser
        tab = next((t for t in await browser.tabs() if t["targetId"] == self.tab), None)
        if tab is None:
            return Outcome(
                True, f"{result}\n\n(Вкладка закрылась — открой адрес снова: browser_open.)", detail
            )
        page = await self._snap(tab)
        if self.page is not None and self.page.target == page.target:
            page.origin = self.page.origin
        self.page = page
        body = pages.render(page, [tab], browser.take_notes())
        return Outcome(True, f"{result}\n\n{untrusted(PAGE_LABEL, body)}", detail)

    # --- разрешения ---------------------------------------------------------------------------------------
    def _autonomy(self, run: "Session") -> str:
        return str(run.ctx.settings.value.background.autonomy or "normal")

    def _needs_ok(self, run: "Session", name: str) -> bool:
        """Нажатие «name» — только с разрешения? Деньги — всегда; публикация и отправка, удаление — кроме
        «Спрашивать реже» (публикация — и тогда); остальное рискованное — при «Спрашивать чаще»."""
        autonomy = self._autonomy(run)
        if MONEY.search(name) or SEND_NAME.search(name):
            return True
        if DELETE.search(name):
            return autonomy != "bold"
        return autonomy == "careful" and bool(RISKY_NAME.search(name))

    async def _ok(self, run: "Session", title: str, detail: str, key: str) -> None:
        await run.service.approve(run, title, detail, key)

    def _messenger(self, run: Any, page: pages.Page | None) -> bool:
        return page is not None and bool(MESSENGER_HOSTS.search(page.host))

    # --- действия
    # --------------------------------------------------------------------------------------------
    async def _open(self, run: "Session", args: dict[str, Any]) -> Outcome:
        url = _url(str(args.get("url") or ""))
        if not url or not url.startswith(("http://", "https://")):
            return Outcome(False, "Ошибка: нужен адрес сайта https://…")
        browser, _ = await self._front(run)
        session = await browser.session(self.tab)
        if session in browser.dialogs:
            raise UserFacingError(
                "поверх страницы окно сайта — ответь на него: browser_press enter или escape"
            )
        reply = await browser.send("Page.navigate", {"url": url}, session=session, timeout=30.0)
        error = str(reply.get("errorText") or "")
        if error:
            raise UserFacingError(f"сайт не открылся: {_net_error(error)}")
        await self._settle(run, browser, session, LOAD_TIMEOUT_S, started=True)
        site = pages.site_of(url) or url
        outcome = await self._done(run, f"Открыто: {url}", f"Открыл {site}")
        if self.page is not None:
            run.task.add_source(self.page.title or site, self.page.url or url)
        return outcome

    async def _click(self, run: "Session", args: dict[str, Any]) -> Outcome:
        browser, _ = await self._front(run)
        page, element = self._element(args)
        session = await self._session(browser, page)
        name = element.name or element.kind
        site = self._site(page)
        if element.download or element.file:
            return Outcome(False, NO_FILES)
        pending = self.unsent.get(page.host, "") if self._messenger(run, page) else ""
        if pending and (SEND_NAME.search(name) or not element.name):
            await self._ok(
                run,
                f"Отправить сообщение на {site}?",
                f"Будет отправлено: «{one_line(pending, 160)}»",
                f"send:{page.host}:{pending}",
            )
            self.unsent.pop(page.host, None)
        elif self._needs_ok(run, name):
            await self._ok(
                run,
                f"Нажать «{one_line(name, 60)}» на {site}?",
                self._where(page),
                f"click:{page.host}:{name}",
            )
        result = await self._press_element(
            run, browser, page, session, element, double=bool(args.get("double"))
        )
        if result != "dialog":
            await self._settle(run, browser, session, CLICK_SETTLE_S)
        return await self._done(
            run, f"«{one_line(name, 80)}»: {_said(result)}", f"Нажал «{one_line(name, 50)}» на {site}"
        )

    async def _type(self, run: "Session", args: dict[str, Any]) -> Outcome:
        browser, _ = await self._front(run)
        page, element = self._element(args)
        session = await self._session(browser, page)
        text = str(args.get("text") if args.get("text") is not None else "")
        name = element.name or element.kind
        site = self._site(page)
        select = element.tag == "SELECT"
        if not (element.is_field or select):
            raise UserFacingError(f"{element.id} — {element.kind}, а не поле ввода: нажми его browser_click")
        if element.disabled or (element.readonly and not select):
            raise UserFacingError(f"поле «{name}» сейчас недоступно для ввода")
        if element.password or element.card or has_card_number(text):
            return Outcome(False, SECRET_FIELD)
        if self._autonomy(run) == "careful" and element.role != "searchbox":
            await self._ok(
                run,
                f"Ввести текст в «{one_line(name, 40)}» на {site}?",
                f"Текст: «{one_line(text, 120)}»",
                f"type:{page.host}:{name}:{text}",
            )
        if select:
            chosen = await self._select(browser, session, element, text)
            result = f"Выбрано в «{one_line(name, 60)}»: «{one_line(chosen, 60)}»"
        else:
            await self._fill(browser, session, element, text)
            result = f"Поле «{one_line(name, 60)}»: текст введён"
            if self._messenger(run, page) and element.role != "searchbox":
                self.unsent[page.host] = text
        if args.get("submit"):
            pending = self.unsent.get(page.host, "")
            if pending:
                await self._ok(
                    run,
                    f"Отправить сообщение на {site}?",
                    f"Будет отправлено: «{one_line(pending, 160)}»",
                    f"send:{page.host}:{pending}",
                )
                self.unsent.pop(page.host, None)
            if await self._key(browser, session, "enter") == "dialog":
                return await self._done(run, f"{result}; Enter нажат — сайт показал окно")
            await self._settle(run, browser, session, CLICK_SETTLE_S)
            result += "; Enter нажат"
        return await self._done(run, result, f"Ввёл текст на {site}")

    async def _press(self, run: "Session", args: dict[str, Any]) -> Outcome:
        combo = normalize_combo(str(args.get("keys") or args.get("combo") or ""))
        if not combo:
            return Outcome(False, "Ошибка: не указаны клавиши")
        browser, _ = await self._front(run)
        session = await browser.session(self.tab)
        page = self.page
        site = self._site(page)
        dialog = browser.dialogs.get(session)
        if dialog is not None:
            return await self._answer_dialog(run, browser, session, dialog, combo)
        if combo in BACK_KEYS or combo in FORWARD_KEYS:
            return await self._history(run, browser, session, -1 if combo in BACK_KEYS else 1)
        if combo in RELOAD_KEYS:
            await browser.send("Page.reload", {"ignoreCache": combo != "f5"}, session=session)
            await self._settle(run, browser, session, LOAD_TIMEOUT_S, started=True)
            return await self._done(run, "Страница обновлена")
        if combo in CLOSE_TAB_KEYS or combo in ("ctrl+t", "ctrl+n") or combo in PASTE_KEYS:
            return Outcome(False, "Ошибка: у задачи одна вкладка — новый адрес открывай browser_open")
        pending = self.unsent.get(page.host, "") if page is not None and combo == "enter" else ""
        if pending:
            await self._ok(
                run,
                f"Отправить сообщение на {site}?",
                f"Будет отправлено: «{one_line(pending, 160)}»",
                f"send:{page.host if page else ''}:{pending}",
            )
            self.unsent.pop(page.host if page else "", None)
        focused = page.find(page.focused) if page is not None and page.focused else None
        if combo in ("enter", "space") and focused is not None and not focused.is_field:
            if focused.download or focused.file:
                return Outcome(False, NO_FILES)
            if self._needs_ok(run, focused.name or ""):
                await self._ok(
                    run,
                    f"Нажать «{one_line(focused.name, 60)}» на {site}?",
                    self._where(page),
                    f"click:{page.host if page else ''}:{focused.name}",
                )
        if await self._key(browser, session, combo) == "dialog":
            return await self._done(run, f"Нажато {combo} — сайт показал окно")
        await self._settle(run, browser, session, CLICK_SETTLE_S)
        return await self._done(run, f"Нажато {combo}")

    async def _answer_dialog(
        self, run: "Session", browser: AgentBrowser, session: str, dialog: dict[str, Any], combo: str
    ) -> Outcome:
        if combo not in ("enter", "escape"):
            return Outcome(False, f"Ошибка: {pages.dialog_text(dialog)}")
        accept = combo == "enter"
        message = one_line(str(dialog.get("message") or ""), 160)
        site = pages.site_of(str(dialog.get("url") or "")) or self._site(self.page)
        if accept and (str(dialog.get("type")) == "beforeunload" or self._needs_ok(run, message)):
            await self._ok(
                run, f"Согласиться на {site}: «{message}»?", f"Окно сайта {site}", f"dialog:{site}:{message}"
            )
        await browser.send("Page.handleJavaScriptDialog", {"accept": accept}, session=session)
        browser.dialogs.pop(session, None)
        await self._settle(run, browser, session, CLICK_SETTLE_S)
        return await self._done(run, "Окно сайта: " + ("OK" if accept else "Отмена"))

    async def _screenshot(self, run: "Session", args: dict[str, Any]) -> Outcome:
        browser, _ = await self._front(run)
        session = await browser.session(self.tab)
        try:
            shot = await browser.send(
                "Page.captureScreenshot", {"format": "jpeg", "quality": 75}, session=session, timeout=15.0
            )
        except CdpError as e:
            raise UserFacingError(f"картинка страницы не снялась: {e.message}") from e
        data = str(shot.get("data") or "")
        if not data:
            return Outcome(False, "Ошибка: картинка страницы пустая")
        message = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Картинка видимой части страницы (только посмотреть: нажимать — по id).",
                },
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}},
            ],
        }
        return Outcome(True, "Картинка страницы приложена.", "Смотрю на страницу", [message])

    # --- инструменты задачи ------------------------------------------------------------------------------
    def tools(self, *, vision: bool = False) -> list["TaskTool"]:
        from jarvis.background.tools import TaskTool

        def element(args: dict[str, Any]) -> str:
            page = self.page
            found = page.find(str(args.get("id") or "")) if page is not None else None
            return one_line(found.name or found.kind, 50) if found is not None else str(args.get("id") or "")

        s = {"type": "string"}
        tools = [
            TaskTool(
                "browser_open",
                f"Открыть сайт во вкладке задачи: url — адрес https://…. {TASK_BROWSER_RULES}",
                {"url": {**s, "description": "Адрес сайта"}},
                ["url"],
                self._open,
                lambda a: f"Открываю {pages.site_of(_url(str(a.get('url') or ''))) or a.get('url')}",
                timeout=60.0,
                risk="act",
            ),
            TaskTool(
                "browser_click",
                "Нажать элемент страницы по id (b12): ссылку, кнопку, флажок; элемент вне экрана браузер "
                "прокрутит к себе сам. double — двойной щелчок.",
                {"id": {**s, "description": "id элемента, например b12"}, "double": {"type": "boolean"}},
                ["id"],
                self._click,
                lambda a: f"Нажимаю «{element(a)}»",
                timeout=60.0,
                risk="act",
            ),
            TaskTool(
                "browser_type",
                "Ввести текст в поле страницы по id (b5): прежнее содержимое заменится; в списке (select) — "
                "выбрать вариант. submit — потом нажать Enter (поиск, отправка формы).",
                {"id": {**s, "description": "id поля"}, "text": s, "submit": {"type": "boolean"}},
                ["id", "text"],
                self._type,
                lambda a: f"Ввожу текст в «{element(a)}»",
                timeout=60.0,
                risk="act",
            ),
            TaskTool(
                "browser_scroll",
                "Прокрутить страницу: direction down / up / top / bottom, amount — экранов (1–10); id — "
                "прокрутить к элементу.",
                {
                    "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
                    "amount": {"type": "integer"},
                    "id": s,
                },
                [],
                self._scroll,
                lambda a: "Листаю страницу",
            ),
            TaskTool(
                "browser_press",
                "Нажать клавиши в странице: enter, escape, tab, стрелки, page_down…; alt+left — назад, "
                "alt+right — вперёд, f5 — обновить. Окно сайта поверх страницы: enter — OK, escape — Отмена.",
                {"keys": {**s, "description": "Клавиши через +"}},
                ["keys"],
                self._press,
                lambda a: f"Нажимаю {a.get('keys')}",
                risk="act",
            ),
            TaskTool(
                "browser_wait",
                "Подождать на странице: text — пока не появится этот текст (до seconds, по умолчанию 10 с); "
                "без text — seconds секунд (до 15).",
                {"seconds": {"type": "number"}, "text": s},
                [],
                self._wait,
                lambda a: "Жду страницу",
                timeout=40.0,
            ),
            TaskTool(
                "browser_read",
                "Прочитать страницу целиком — текст и элементы с id — частями (part 1, 2…). Ничего не "
                "нажимает.",
                {"part": {"type": "integer"}},
                [],
                self._read,
                lambda a: "Читаю страницу",
                timeout=40.0,
            ),
        ]
        if vision:
            tools.append(
                TaskTool(
                    "browser_screenshot",
                    "Картинка видимой части страницы — посмотреть то, чего нет в тексте (графики, картинки).",
                    {},
                    [],
                    self._screenshot,
                    lambda a: "Смотрю на страницу",
                    timeout=30.0,
                )
            )
        return tools

    async def release(self, run: "Session") -> None:
        """Заход закончился: задача ждёт ответа — вкладка остаётся (человек, может быть, войдёт в ней на
        сайт); иначе — закрыть."""
        if run.outcome == "waiting" or (run.parked and not run.closing):
            return
        with contextlib.suppress(Exception):
            await self.agent_browser.close_task(run.task.id)
