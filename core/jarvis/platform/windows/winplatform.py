"""WindowsPlatform — настоящая платформа: звук, медиа, ввод, программы, окна, система.

Все методы вызываются из потока платформы (AppContext.run_platform: один поток, COM в режиме STA), поэтому
COM-объекты (громкость, медиасессии, ярлыки) кэшируются для потока. Конструктор лёгкий и работает на любой ОС:
Windows-библиотеки подключаются при первом вызове метода. Любая неожиданная ошибка превращается
в PlatformError с коротким текстом для человека, подробности — в журнал.
"""

import contextlib
import functools
import logging
import ntpath
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psutil

from jarvis import paths
from jarvis.platform.base import (
    AppInfo,
    FileInfo,
    FileQuery,
    IndexedFile,
    MediaInfo,
    MediaKey,
    MouseButton,
    PlatformError,
    PowerAction,
    SystemStats,
    WindowAction,
    WindowInfo,
)
from jarvis.platform.windows import apps as appmod
from jarvis.platform.windows import files, inputs, keys, screen, system
from jarvis.platform.windows import winapi as wa
from jarvis.platform.windows.audio import Audio, NoAudioDevice, volume_key_presses
from jarvis.platform.windows.media import Media
from jarvis.platform.windows.procs import (
    OwnProcesses,
    is_protected,
    match_process_names,
    process_table,
)
from jarvis.platform.windows.windowing import WindowApi, title_matches_app

log = logging.getLogger(__name__)

PASTE_RESTORE_S = 1.0  # через сколько вернуть буфер обмена после Ctrl+V (программа читает его не сразу)
KILL_WAIT_S = 3.0
SYSTEM_SOUNDS = frozenset({"system", "system sounds", "системные звуки", "система"})
MEDIA_VKS = {
    "play_pause": keys.VK_MEDIA_PLAY_PAUSE,
    "next": keys.VK_MEDIA_NEXT_TRACK,
    "prev": keys.VK_MEDIA_PREV_TRACK,
    "stop": keys.VK_MEDIA_STOP,
}
# схемы ссылок, которыми запускают код или открывают чужие файлы, — не открываем
DENIED_SCHEMES = frozenset(
    {"ms-msdt", "search-ms", "search", "ms-officecmd", "javascript", "vbscript", "data", "file", "hcp"}
    | {"ms-cxh", "ms-cxh-full"}
)
SHELL_ERRORS = {
    0: "Не хватает памяти, чтобы открыть «{0}»",
    2: "Не найдено: «{0}»",
    3: "Не найдено: «{0}»",
    5: "Нет доступа к «{0}» (или запуск отменён)",
    8: "Не хватает памяти, чтобы открыть «{0}»",
    11: "«{0}» — не программа Windows",
    26: "Нет программы, которая открывает «{0}»",
    27: "Нет программы, которая открывает «{0}»",
    28: "Программа не ответила при открытии «{0}»",
    29: "Программа не ответила при открытии «{0}»",
    30: "Программа не ответила при открытии «{0}»",
    31: "Нет программы, которая открывает «{0}»",
}
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+):")
_DOMAIN = re.compile(r"^(?:[\w-]+\.)+[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#]\S*)?$")
_SIMPLE_NAME = re.compile(r"^[\w.\-]+$")


def _guard[**P, R](message: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """PlatformError — как есть; прочие ошибки — в журнал и PlatformError(message)."""

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except PlatformError:
                raise
            except Exception as e:
                log.warning("%s не выполнен: %r", fn.__name__, e, exc_info=True)
                raise PlatformError(message) from e

        return wrapper

    return decorate


def _clamp(level: int) -> int:
    return max(0, min(100, int(level)))


def _short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path.strip().strip('"').strip()))


@dataclass
class _PendingRestore:
    snapshot: list[tuple[int, bytes]]
    sequence: int
    timer: threading.Timer | None = None


class WindowsPlatform:
    name = "windows"

    def __init__(self) -> None:
        if sys.platform == "win32":
            wa.set_process_dpi_aware()
        self._lock = threading.Lock()
        self._own = OwnProcesses()
        self._apps = appmod.AppInventory(
            lambda: paths.cache_dir() / "apps.json", appmod.scan_inventory, appmod.com_thread_init
        )
        self._audio = Audio()
        self._media = Media()
        self._stats = system.Stats()
        self._brightness = system.Brightness()
        self._sender: inputs.Sender | None = None
        self._windows: WindowApi | None = None
        self._clipboard: system.Clipboard | None = None
        self._local = threading.local()
        self._paste_lock = threading.Lock()
        self._paste_pending: _PendingRestore | None = None
        self._power_timers: list[threading.Timer] = []
        self._last_volume: int | None = None
        self._last_muted = False

    # --- ленивые части ----------------------------------------------------------------------------------

    def _input(self) -> inputs.Sender:
        with self._lock:
            if self._sender is None:
                self._sender = inputs.Sender()
            return self._sender

    def _win(self) -> WindowApi:
        with self._lock:
            if self._windows is None:
                self._windows = WindowApi(self._own, self._apps.peek, self._input)
            return self._windows

    def _cb(self) -> system.Clipboard:
        with self._lock:
            if self._clipboard is None:
                self._clipboard = system.Clipboard()
            return self._clipboard

    def _shortcuts(self) -> appmod.ShortcutReader:
        reader = getattr(self._local, "shortcuts", None)
        if reader is None:
            reader = appmod.ShortcutReader()
            self._local.shortcuts = reader
        return reader

    def virtual_screen(self) -> tuple[int, int, int, int]:
        """Виртуальный экран (все мониторы): left, top, width, height. Снимок screenshot() — ровно он:
        точка снимка (px, py) — это точка экрана (left + px, top + py)."""
        return self._input().virtual_screen()

    @_guard("Не удалось узнать монитор окна")
    def monitor_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        """Монитор, на котором окно (или ближайший к нему): left, top, right, bottom — в точках снимка."""
        return self._win().monitor_rect(int(hwnd))

    @_guard("Не удалось узнать мониторы")
    def monitors(self) -> list[tuple[int, int, int, int]]:
        """Все мониторы: left, top, right, bottom — в точках снимка."""
        return screen.monitors()

    # --- звук ---------------------------------------------------------------------------------------------

    def _volume_keys(self, down: int, up: int) -> None:
        sender = self._input()
        events: list[Any] = []
        for vk, count in ((keys.VK_VOLUME_DOWN, down), (keys.VK_VOLUME_UP, up)):
            for _ in range(max(0, count)):
                events += [sender.key(vk), sender.key(vk, up=True)]
        sender.send_all(events, "клавиши громкости")

    @_guard("Не удалось узнать громкость")
    def volume_get(self) -> int:
        try:
            level = self._audio.get_level()
        except NoAudioDevice as e:
            raise PlatformError("Нет устройства вывода звука") from e
        self._last_volume = level
        return level

    @_guard("Не удалось изменить громкость")
    def volume_set(self, level: int) -> int:
        level = _clamp(level)
        try:
            value = self._audio.set_level(level, unmute=level > 0)
        except NoAudioDevice as e:
            raise PlatformError("Нет устройства вывода звука") from e
        except Exception as e:
            log.warning("громкость через COM не изменена (%r) — клавишами громкости", e)
            self._volume_keys(*volume_key_presses(level))
            value = level
        self._last_volume = value
        return value

    @_guard("Не удалось изменить громкость")
    def volume_change(self, delta: int) -> int:
        delta = int(delta)
        try:
            value = self._audio.change_level(delta)
        except NoAudioDevice as e:
            raise PlatformError("Нет устройства вывода звука") from e
        except Exception as e:
            log.warning("громкость через COM не изменена (%r) — клавишами громкости", e)
            presses = max(1, round(abs(delta) / 2)) if delta else 0
            self._volume_keys(presses if delta < 0 else 0, presses if delta > 0 else 0)
            value = _clamp((self._last_volume if self._last_volume is not None else 50) + delta)
        self._last_volume = value
        return value

    @_guard("Не удалось включить или выключить звук")
    def mute_set(self, muted: bool | None = None) -> bool:
        try:
            value = self._audio.set_muted(muted)
        except NoAudioDevice as e:
            raise PlatformError("Нет устройства вывода звука") from e
        except Exception as e:
            log.warning("звук через COM не переключён (%r) — клавишей", e)
            value = (not self._last_muted) if muted is None else bool(muted)
            if value != self._last_muted or muted is None:
                sender = self._input()
                sender.send_all([sender.key(keys.VK_VOLUME_MUTE), sender.key(keys.VK_VOLUME_MUTE, up=True)])
        self._last_muted = value
        return value

    @_guard("Не удалось узнать, включён ли звук")
    def is_muted(self) -> bool:
        try:
            value = self._audio.is_muted()
        except NoAudioDevice as e:
            raise PlatformError("Нет устройства вывода звука") from e
        self._last_muted = value
        return value

    @_guard("Не удалось изменить громкость программы")
    def app_volume_set(self, app: str, level: int) -> bool:
        query = (app or "").strip()
        if not query:
            raise PlatformError("Не указана программа")
        level = _clamp(level)
        sessions = self._audio.sessions()
        names: dict[int, str] = {}
        for pid, _control in sessions:
            if pid and pid not in names:
                try:
                    names[pid] = str(psutil.Process(pid).name())
                except psutil.Error:
                    names[pid] = ""
        if query.casefold() in SYSTEM_SOUNDS:
            targets = [control for pid, control in sessions if pid == 0]
        else:
            candidates = appmod.exe_candidates(query, self._apps.peek())
            matched = match_process_names(candidates, query, (n for n in names.values() if n))
            targets = [control for pid, control in sessions if names.get(pid, "").casefold() in matched]
        changed = 0
        for control in targets:
            try:
                self._audio.set_session_level(control, level)
                changed += 1
            except Exception as e:
                log.info("громкость сессии не изменена: %r", e)
        return changed > 0

    @_guard("Не удалось прочитать громкость программ")
    def app_volumes(self) -> list[dict[str, Any]]:
        """Микшер: программы, у которых есть звук, — {app (имя процесса), title, level, muted, active}.
        Несколько сессий одной программы (вкладки браузера) — одна строка."""
        titles = {appmod.app_exe(a): a.name for a in self._apps.peek()}
        own = self._own.pids()
        rows: dict[str, dict[str, Any]] = {}
        for pid, control in self._audio.sessions():
            if pid:
                try:
                    exe = str(psutil.Process(pid).name())
                except psutil.Error:
                    continue
                if not exe:
                    continue
                title = (
                    "Jarvis"
                    if pid in own
                    else titles.get(exe.casefold()) or ntpath.splitext(exe)[0].replace("_", " ").title()
                )
            else:
                exe, title = "system", "Системные звуки"
            try:
                level, muted, active = self._audio.session_volume(control)
            except Exception as e:
                log.debug("громкость сессии %s не прочитана: %r", exe, e)
                continue
            key = exe.casefold()
            row = rows.get(key)
            if row is None:
                rows[key] = {"app": exe, "title": title, "level": level, "muted": muted, "active": active}
            else:
                row["level"] = max(row["level"], level)
                row["muted"] = row["muted"] and muted
                row["active"] = row["active"] or active
        # звучащие сейчас — сверху, системные звуки — в конце
        return sorted(
            rows.values(), key=lambda r: (r["app"] == "system", not r["active"], r["title"].casefold())
        )

    # --- медиа --------------------------------------------------------------------------------------------

    @_guard("Не удалось нажать медиаклавишу")
    def media_key(self, key: MediaKey) -> None:
        vk = MEDIA_VKS.get(str(key))
        if vk is None:
            raise PlatformError(f"Неизвестная медиаклавиша: {key}")
        self._input().combo([vk])

    def now_playing(self) -> MediaInfo | None:
        return self._media.now_playing()

    # --- ввод ---------------------------------------------------------------------------------------------

    @_guard("Не удалось нажать клавиши")
    def press_keys(self, combo: str) -> None:
        vks = keys.parse_combo(combo)
        sender = self._input()
        sender.release_modifiers(wait_s=0.3)
        sender.combo(vks)

    @_guard("Не удалось нажать клавишу")
    def key_down(self, key: str) -> None:
        sender = self._input()
        sender.send_all([sender.key(vk) for vk in keys.parse_combo(key)])

    @_guard("Не удалось отпустить клавишу")
    def key_up(self, key: str) -> None:
        sender = self._input()
        sender.send_all([sender.key(vk, up=True) for vk in reversed(keys.parse_combo(key))])

    @_guard("Не удалось напечатать текст")
    def type_text(self, text: str) -> None:
        if not text:
            return
        sender = self._input()
        sender.release_modifiers(wait_s=inputs.MODIFIERS_WAIT_S)
        sender.type_text(text)

    @_guard("Не удалось вставить текст")
    def paste_text(self, text: str) -> None:
        """Через буфер обмена: сохранить → записать текст → Ctrl+V → через PASTE_RESTORE_S вернуть прежний
        буфер (если его за это время никто не поменял). Временный текст не попадает в журнал буфера Win+V."""
        if not text:
            return
        cb = self._cb()
        sender = self._input()
        with self._paste_lock:
            pending = self._paste_pending
            if pending is not None and pending.timer is not None:
                pending.timer.cancel()  # прошлая вставка ещё не вернула буфер: исходный — её снимок
            snapshot = pending.snapshot if pending is not None else cb.snapshot()
            restore = _PendingRestore(snapshot, cb.set_text(text, transient=True))
            self._paste_pending = restore
            try:
                sender.release_modifiers(wait_s=inputs.MODIFIERS_WAIT_S)
                sender.combo([keys.VK_CONTROL, ord("V")])
            except Exception:
                self._paste_pending = None
                with contextlib.suppress(Exception):
                    cb.restore(snapshot)
                raise
            restore.timer = threading.Timer(PASTE_RESTORE_S, self._restore_clipboard, args=(restore,))
            restore.timer.daemon = True
            restore.timer.start()

    def _restore_clipboard(self, restore: _PendingRestore) -> None:
        with self._paste_lock:
            if self._paste_pending is not restore:
                return
            self._paste_pending = None
            try:
                cb = self._cb()
                if cb.sequence() != restore.sequence:
                    log.info("буфер обмена изменился после вставки — прежний не возвращаю")
                    return
                cb.restore(restore.snapshot)
            except Exception as e:
                log.warning("буфер обмена не восстановлен: %r", e)

    @_guard("Не удалось передвинуть курсор")
    def mouse_move(self, x: int, y: int) -> None:
        self._input().move(int(x), int(y))

    @_guard("Не удалось щёлкнуть мышью")
    def mouse_click(
        self, x: int | None = None, y: int | None = None, button: MouseButton = "left", double: bool = False
    ) -> None:
        sender = self._input()
        if x is not None and y is not None:
            sender.click(int(x), int(y), button, double)
        else:
            sender.click(None, None, button, double)

    @_guard("Не удалось прокрутить")
    def mouse_scroll(self, amount: int) -> None:
        self._input().scroll(int(amount))

    @_guard("Не удалось узнать положение курсора")
    def cursor_pos(self) -> tuple[int, int]:
        return self._input().cursor()

    @_guard("Не удалось узнать размер экрана")
    def screen_size(self) -> tuple[int, int]:
        """Размер основного монитора в физических точках."""
        return self._input().primary_size()

    # --- программы и процессы ---------------------------------------------------------------------------

    @_guard("Не удалось получить список программ")
    def list_apps(self, refresh: bool = False) -> list[AppInfo]:
        return self._apps.get(refresh)

    def _shell_open(self, file: str, args: str = "", cwd: str | None = None, label: str = "") -> None:
        """ShellExecuteW «open»: программа, ярлык, документ, папка или ссылка (аргументы — без оболочки)."""
        w = wa.api()
        w.AllowSetForegroundWindow(wa.ASFW_ANY)
        code = int(w.ShellExecuteW(None, "open", file, args or None, cwd or None, wa.SW_SHOWNORMAL) or 0)
        if code > 32:
            return
        log.warning("ShellExecuteW(%r, %r): код %d", file, args, code)
        template = SHELL_ERRORS.get(code, "Не удалось открыть «{0}»")
        raise PlatformError(template.format(_short(label or file)))

    def _explorer(self, argument: str) -> None:
        """explorer.exe <shell:…> — так запускаются программы из shell:AppsFolder по AppID."""
        root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
        with contextlib.suppress(Exception):
            wa.api().AllowSetForegroundWindow(wa.ASFW_ANY)
        try:
            subprocess.Popen(
                [ntpath.join(root, "explorer.exe"), argument],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=system.NO_WINDOW,
            )
        except OSError as e:
            log.warning("explorer.exe %s: %r", argument, e)
            raise PlatformError("Не удалось запустить программу") from e

    def _launch_app(self, app: AppInfo, args: str = "") -> None:
        exe = app.path if app.path and appmod.exe_name(app.path) and os.path.isfile(app.path) else ""
        if args and exe:
            self._shell_open(exe, args, ntpath.dirname(exe), app.name)
            return
        if args:
            log.info("аргументы для «%s» не переданы: неизвестен путь к exe", app.name)
        target = app.app_id
        if appmod.classify_target(target) == "path" and os.path.exists(_expand(target)):
            self._shell_open(_expand(target), args, label=app.name)
            return
        self._explorer(f"shell:AppsFolder\\{target}")

    @_guard("Не удалось запустить программу")
    def launch(self, target: str, args: str = "") -> None:
        t = (target or "").strip().strip('"').strip()
        if not t:
            raise PlatformError("Не указано, что запустить")
        args = (args or "").strip()
        kind = appmod.classify_target(t)
        if kind == "url":
            self.open_url(t)
            return
        if kind == "shell":
            self._explorer(t)
            return
        if kind == "path":
            path = _expand(t)
            if not os.path.exists(path):
                raise PlatformError(f"Не найдено: «{_short(t)}»")
            self._shell_open(path, args, ntpath.dirname(path) if appmod.exe_name(path) else None)
            return
        if kind == "appid":
            app = appmod.find_by_app_id(t, self._apps.peek()) or AppInfo(t, t)
            self._launch_app(app, args)
            return
        if kind == "exe":
            exe = t.casefold()
            app = next((a for a in self._apps.peek() if appmod.app_exe(a) == exe), None)
            if app is None:
                try:
                    self._shell_open(t, args)  # PATH и App Paths: notepad.exe, chrome.exe
                    return
                except PlatformError:
                    app = next((a for a in self._apps.get() if appmod.app_exe(a) == exe), None)
                    if app is None:
                        raise
            self._launch_app(app, args)
            return
        # AppID обычной программы — без «!»: «Microsoft.Office.EXCEL.EXE.15», «Microsoft.VisualStudioCode»
        # (так их отдаёт меню «Пуск» и запоминают виджеты) — сперва точное совпадение, потом имя
        app = appmod.find_by_app_id(t, self._apps.peek()) or self._apps.resolve(t)
        if app is None:  # resolve мог обновить инвентарь — AppID могло в нём появиться
            app = appmod.find_by_app_id(t, self._apps.peek())
        if app is not None:
            self._launch_app(app, args)
            return
        if _SIMPLE_NAME.match(t):  # «calc», «mspaint», «regedit» — из System32 и App Paths
            with contextlib.suppress(PlatformError):
                self._shell_open(f"{t}.exe", args)
                return
        raise PlatformError(f"Программа «{_short(t, 60)}» не найдена")

    @_guard("Не удалось открыть ссылку")
    def open_url(self, url: str) -> None:
        u = (url or "").strip().strip('"').strip()
        if not u:
            raise PlatformError("Не указана ссылка")
        m = _SCHEME.match(u)
        if m is None:
            if not _DOMAIN.match(u):
                raise PlatformError(f"Это не похоже на ссылку: «{_short(u)}»")
            u = f"https://{u}"
        elif m.group(1).casefold() in DENIED_SCHEMES:
            raise PlatformError("Такие ссылки Jarvis не открывает")
        self._shell_open(u.replace(" ", "%20"), label=u)

    @_guard("Не удалось открыть")
    def open_path(self, path: str) -> None:
        p = _expand(path or "")
        if not p:
            raise PlatformError("Не указан путь")
        if not os.path.exists(p):
            raise PlatformError(f"Не найдено: «{_short(path)}»")
        self._shell_open(p)

    @_guard("Не удалось закрыть программу")
    def close_app(self, name: str) -> int:
        """Закрыть окна программы (WM_CLOSE, как крестик): по имени процесса, программы или заголовку."""
        query = (name or "").strip()
        if not query:
            raise PlatformError("Не указана программа")
        win = self._win()
        others, _mine = win.enum()
        candidates = appmod.exe_candidates(query, self._apps.peek())
        matched = match_process_names(candidates, query, {w.exe for w in others if w.exe})
        targets = [w for w in others if w.exe.casefold() in matched]
        if not targets:
            targets = [w for w in others if title_matches_app(w.title, query)]
        return sum(1 for w in targets if win.close(w.hwnd))

    @_guard("Не удалось завершить процесс")
    def kill_process(self, name: str) -> int:
        """Завершить процессы программы. Свои процессы и системные — никогда (PlatformError)."""
        query = (name or "").strip()
        if not query:
            raise PlatformError("Не указан процесс")
        table = process_table()
        if query.isdigit():
            targets = [(pid, n) for pid, n in table if pid == int(query)]
        else:
            candidates = appmod.exe_candidates(query, self._apps.peek())
            matched = match_process_names(candidates, query, (n for _pid, n in table))
            targets = [(pid, n) for pid, n in table if n.casefold() in matched]
        if not targets:
            return 0
        own = self._own.pids()
        foreign = [(pid, n) for pid, n in targets if pid not in own]
        if not foreign:
            raise PlatformError("Это сам Jarvis — его так не завершить")
        allowed = [(pid, n) for pid, n in foreign if not is_protected(n, pid)]
        if not allowed:
            raise PlatformError(f"«{foreign[0][1]}» — системный процесс, завершать его нельзя")
        killed: list[psutil.Process] = []
        denied = 0
        for pid, exe in allowed:
            try:
                proc = psutil.Process(pid)
                if proc.name().casefold() != exe.casefold():  # pid уже занят другим процессом
                    continue
                proc.kill()
                killed.append(proc)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                denied += 1
        gone, _alive = psutil.wait_procs(killed, timeout=KILL_WAIT_S) if killed else ([], [])
        if not gone and denied:
            raise PlatformError(f"Нет прав завершить «{allowed[0][1]}» (запущен от администратора?)")
        return len(gone)

    @_guard("Не удалось проверить процессы")
    def is_running(self, name: str) -> bool:
        query = (name or "").strip()
        if not query:
            return False
        own = self._own.pids()
        names = {n for pid, n in process_table() if pid not in own}
        candidates = appmod.exe_candidates(query, self._apps.peek())
        return bool(match_process_names(candidates, query, names))

    # --- окна ---------------------------------------------------------------------------------------------

    @_guard("Не удалось получить список окон")
    def list_windows(self) -> list[WindowInfo]:
        return self._win().list_windows()

    @_guard("Не удалось узнать активное окно")
    def foreground_window(self) -> WindowInfo | None:
        return self._win().foreground()

    def _minimize_all(self) -> None:
        with contextlib.suppress(Exception):
            import win32com.client  # type: ignore[import-not-found]

            win32com.client.Dispatch("Shell.Application").MinimizeAll()
            return
        w = wa.api()
        tray = w.FindWindowW("Shell_TrayWnd", None)
        if tray and w.PostMessageW(tray, wa.WM_COMMAND, wa.TRAY_MINIMIZE_ALL, 0):
            return
        self._input().combo([keys.VK_LWIN, ord("M")])

    def _show_desktop(self) -> None:
        with contextlib.suppress(Exception):
            import win32com.client  # type: ignore[import-not-found]

            win32com.client.Dispatch("Shell.Application").ToggleDesktop()
            return
        self._input().combo([keys.VK_LWIN, ord("D")])

    @_guard("Не удалось выполнить действие с окном")
    def window_action(self, action: WindowAction, target: int | str | None = None) -> None:
        if action == "minimize_all":
            self._minimize_all()
            return
        if action == "show_desktop":
            self._show_desktop()
            return
        if action not in ("minimize", "maximize", "restore", "close", "focus"):
            raise PlatformError(f"Неизвестное действие с окном: {action}")
        win = self._win()
        info = win.find(target)
        if action == "minimize":
            win.show(info.hwnd, wa.SW_MINIMIZE)
        elif action == "close":
            if not win.close(info.hwnd):
                raise PlatformError("Не удалось закрыть окно")
        elif action == "focus":
            win.focus(info.hwnd)
        else:
            if win.is_hung(info.hwnd):
                raise PlatformError("Окно не отвечает")
            win.w.ShowWindow(info.hwnd, wa.SW_MAXIMIZE if action == "maximize" else wa.SW_RESTORE)
            with contextlib.suppress(PlatformError):  # развёрнутое окно человек хочет видеть
                win.focus(info.hwnd)

    def is_fullscreen_app(self) -> bool:
        try:
            return self._win().is_fullscreen_foreground()
        except Exception as e:
            log.debug("проверка полноэкранного окна: %r", e)
            return False

    # --- система ------------------------------------------------------------------------------------------

    def _later(self, delay_s: float, fn: Callable[[], None], what: str) -> None:
        def run() -> None:
            try:
                fn()
            except Exception as e:
                log.warning("%s не выполнено: %r", what, e)

        timer = threading.Timer(delay_s, run)
        timer.daemon = True
        with self._lock:
            self._power_timers = [t for t in self._power_timers if t.is_alive()] + [timer]
        timer.start()

    @_guard("Не удалось выполнить команду питания")
    def power(self, action: PowerAction, delay_s: int = 0) -> None:
        delay = max(0, int(delay_s or 0))
        if action == "cancel":
            with self._lock:
                timers, self._power_timers = self._power_timers, []
            for timer in timers:
                timer.cancel()
            system.run_shutdown("cancel")
            return
        if action in ("shutdown", "restart"):
            system.run_shutdown(action, delay)
            return
        if action == "lock":
            if delay:
                self._later(delay, system.lock_workstation, "блокировка")
            else:
                system.lock_workstation()
            return
        if action == "logoff":
            if delay:
                self._later(delay, lambda: system.run_shutdown("logoff"), "выход из системы")
            else:
                system.run_shutdown("logoff")
            return
        if action in ("sleep", "hibernate"):
            hibernate = action == "hibernate"
            system.check_suspend(hibernate)
            # SetSuspendState возвращается только после пробуждения — не держим поток платформы
            self._later(max(delay, 0.3), lambda: system.suspend(hibernate), "сон")
            return
        raise PlatformError(f"Неизвестное действие питания: {action}")

    @_guard("Не удалось снять экран")
    def screen_pixels(self, rect: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]:
        """BGRA экрана или прямоугольника — без PNG (агент смотрит на экран после каждого действия)."""
        bgra, width, height, _shot = screen.grab_pixels(rect)
        return bgra, width, height

    @_guard("Не удалось сделать снимок")
    def screenshot(self, window: int | None = None) -> bytes:
        if window is None:
            return screen.grab_screen()
        return screen.grab_window(int(window), self._win())

    @_guard("Не удалось прочитать буфер обмена")
    def clipboard_get(self) -> str:
        return self._cb().get_text()

    @_guard("Не удалось записать в буфер обмена")
    def clipboard_set(self, text: str) -> None:
        self._cb().set_text(text or "")

    @_guard("Не удалось прочитать буфер обмена")
    def clipboard_seq(self) -> int:
        return self._cb().sequence()

    @_guard("Не удалось прочитать буфер обмена")
    def clipboard_get_item(self) -> tuple[str, bool]:
        return self._cb().get_item()

    @_guard("Не удалось сохранить буфер обмена")
    def clipboard_snapshot(self) -> Any:
        """Все форматы буфера. Пока прошлая вставка (paste_text) не вернула буфер человека, в нём временный
        текст — тогда настоящий буфер — снимок этой вставки (её таймер увидит новую версию и не тронет)."""
        with self._paste_lock:
            pending = self._paste_pending
        if pending is not None:
            return list(pending.snapshot)
        return self._cb().snapshot()

    @_guard("Не удалось вернуть буфер обмена")
    def clipboard_restore(self, snapshot: Any) -> None:
        self._cb().restore([(int(fmt), bytes(data)) for fmt, data in snapshot or []])

    @_guard("Не удалось узнать яркость")
    def brightness_get(self) -> int | None:
        return self._brightness.get()

    @_guard("Не удалось изменить яркость")
    def brightness_set(self, level: int) -> None:
        self._brightness.set(_clamp(level))

    @_guard("Не удалось узнать, что в корзине")
    def recycle_bin_count(self) -> int:
        return system.recycle_bin_count()

    @_guard("Не удалось очистить корзину")
    def empty_recycle_bin(self) -> None:
        system.empty_recycle_bin()

    @_guard("Не удалось переместить в корзину")
    def trash(self, path: str) -> None:
        p = _expand(path or "")
        if not p:
            raise PlatformError("Не указан путь")
        if not os.path.exists(p):
            raise PlatformError(f"Не найдено: «{_short(path)}»")
        from send2trash import send2trash

        send2trash(p)

    @_guard("Не удалось узнать нагрузку")
    def system_stats(self) -> SystemStats:
        return self._stats.collect()

    @_guard("Не удалось получить недавние файлы")
    def recent_files(self, limit: int = 10) -> list[FileInfo]:
        limit = max(1, min(100, int(limit)))
        reader = self._shortcuts()
        return system.select_recent(system.recent_links(), limit, lambda lnk: reader.read(lnk)[0])

    @_guard("Не удалось узнать время простоя")
    def idle_seconds(self) -> float:
        return system.idle_seconds()

    def known_folder(self, name: str) -> str | None:
        return system.known_folder(name or "")

    # --- файлы --------------------------------------------------------------------------------------------

    @_guard(files.UNAVAILABLE)
    def search_index(self, query: FileQuery) -> list[IndexedFile]:
        """Поиск по индексу Windows (OLE DB, Search.CollatorDSO); индекса нет — PlatformError."""
        return files.run_query(files.build_sql(query), max(1, int(query.limit or 50)))

    @_guard("Не удалось показать файл в папке")
    def reveal_path(self, path: str) -> None:
        p = _expand(path or "")
        if not p:
            raise PlatformError("Не указан путь")
        if not os.path.exists(p):
            raise PlatformError(f"Не найдено: «{_short(path)}»")
        with contextlib.suppress(Exception):
            wa.api().AllowSetForegroundWindow(wa.ASFW_ANY)
        files.reveal(p)
