"""Система: буфер обмена, питание, яркость, корзина, нагрузка, недавние файлы, простой, известные папки.

Чистые помощники (пересчёт скоростей сети, загрузка GPU по счётчикам PDH, разбор вывода PowerShell, выбор
недавних файлов, аргументы shutdown.exe, форматы буфера обмена) проверяются тестами на Linux; остальное —
только Windows (ctypes, pywin32, psutil).
"""

import contextlib
import ctypes
import logging
import ntpath
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import psutil

from jarvis.platform.base import FileInfo, PlatformError, SystemStats
from jarvis.platform.windows import winapi as wa

log = logging.getLogger(__name__)

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# --- известные папки ---------------------------------------------------------------------------------

KNOWN_FOLDERS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
}
_FOLDER_ALIASES = {
    "download": "downloads",
    "загрузки": "downloads",
    "document": "documents",
    "документы": "documents",
    "рабочий стол": "desktop",
    "picture": "pictures",
    "images": "pictures",
    "изображения": "pictures",
    "картинки": "pictures",
    "музыка": "music",
    "video": "videos",
    "видео": "videos",
    "home": "profile",
}


def folder_id(name: str) -> str | None:
    key = " ".join(name.strip().casefold().replace("ё", "е").split())
    key = _FOLDER_ALIASES.get(key, key)
    return KNOWN_FOLDERS.get(key)


def known_folder(name: str) -> str | None:
    fid = folder_id(name)
    if fid is None:
        return None
    try:
        return wa.known_folder_path(fid)
    except OSError as e:
        log.warning("известная папка %s: %s", name, e)
        return None


# --- нагрузка ------------------------------------------------------------------------------------------

NetSample = tuple[float, int, int]  # monotonic, bytes_recv, bytes_sent
GPU_COUNTER = r"\GPU Engine(*engtype_3D)\Utilization Percentage"
GPU_RETRY_S = 300.0
_LUID = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", re.IGNORECASE)
_ENGINE = re.compile(r"phys_(\d+)_eng_(\d+)", re.IGNORECASE)


def net_rates(prev: NetSample | None, cur: NetSample) -> tuple[float, float]:
    """Скорость сети в килобитах в секунду (приём, отдача) между двумя замерами; первый замер — нули."""
    if prev is None:
        return 0.0, 0.0
    dt = cur[0] - prev[0]
    if dt <= 0:
        return 0.0, 0.0
    down = max(0, cur[1] - prev[1]) * 8 / 1000 / dt
    up = max(0, cur[2] - prev[2]) * 8 / 1000 / dt
    return round(down, 1), round(up, 1)


def gpu_load(items: dict[str, float]) -> float | None:
    """Загрузка GPU по экземплярам «pid_…_luid_…_phys_0_eng_0_engtype_3D»: сумма процессов по каждому
    3D-движку каждого адаптера, итог — самый загруженный (как график «3D» в диспетчере задач), 0..100."""
    if not items:
        return None
    totals: dict[tuple[str, str], float] = {}
    for name, value in items.items():
        m = _LUID.search(name)
        if not m:
            continue
        engine = _ENGINE.search(name)
        key = (m.group(1).casefold(), engine.group(0) if engine else "")
        with contextlib.suppress(TypeError, ValueError):
            totals[key] = totals.get(key, 0.0) + max(0.0, float(value))
    if not totals:
        return None
    return round(min(100.0, max(totals.values())), 1)


def system_drive() -> str:
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    return drive.rstrip("\\/") + "\\"


class Stats:
    """psutil + счётчики PDH для GPU. Запрос PDH открывается один раз и переиспользуется."""

    def __init__(self) -> None:
        self._net: NetSample | None = None
        self._pdh_query: Any = None
        self._pdh_counter: Any = None
        self._pdh_retry_at = 0.0
        with contextlib.suppress(Exception):
            psutil.cpu_percent(interval=None)  # первый замер — точка отсчёта

    def collect(self) -> SystemStats:
        stats = SystemStats()
        with contextlib.suppress(Exception):
            stats.cpu = round(float(psutil.cpu_percent(interval=None)), 1)
        with contextlib.suppress(Exception):
            vm = psutil.virtual_memory()
            stats.ram = round(float(vm.percent), 1)
            stats.ram_total_gb = round(vm.total / 1024**3, 1)
            stats.ram_used_gb = round((vm.total - vm.available) / 1024**3, 1)
        with contextlib.suppress(Exception):
            disk = psutil.disk_usage(system_drive())
            stats.disk = round(float(disk.percent), 1)
            stats.extra["disk_free_gb"] = round(disk.free / 1024**3, 1)
            stats.extra["disk_total_gb"] = round(disk.total / 1024**3, 1)
        with contextlib.suppress(Exception):
            counters = psutil.net_io_counters()
            sample = (time.monotonic(), int(counters.bytes_recv), int(counters.bytes_sent))
            stats.net_down_kbps, stats.net_up_kbps = net_rates(self._net, sample)
            self._net = sample
        with contextlib.suppress(Exception):
            battery = psutil.sensors_battery()
            if battery is not None:
                stats.battery = round(float(battery.percent), 1)
                stats.charging = bool(battery.power_plugged) if battery.power_plugged is not None else None
        with contextlib.suppress(Exception):
            stats.uptime_s = round(time.time() - psutil.boot_time(), 1)
        with contextlib.suppress(Exception):
            stats.extra["cpu_count"] = psutil.cpu_count() or 0
        stats.gpu = self._gpu()
        return stats

    def _gpu(self) -> float | None:
        if self._pdh_query is None:
            if time.monotonic() < self._pdh_retry_at:
                return None
            try:
                import win32pdh  # type: ignore[import-not-found]

                query = win32pdh.OpenQuery()
                try:
                    counter = win32pdh.AddEnglishCounter(query, GPU_COUNTER)
                    win32pdh.CollectQueryData(query)  # первый замер: значения — со второго
                except Exception:
                    win32pdh.CloseQuery(query)
                    raise
                self._pdh_query, self._pdh_counter = query, counter
            except Exception as e:
                log.info("счётчики GPU недоступны: %s", e)
                self._pdh_retry_at = time.monotonic() + GPU_RETRY_S
            return None
        try:
            import win32pdh  # type: ignore[import-not-found]

            win32pdh.CollectQueryData(self._pdh_query)
            items = win32pdh.GetFormattedCounterArray(self._pdh_counter, win32pdh.PDH_FMT_DOUBLE)
        except Exception as e:  # экземпляры появились между замерами — в следующий раз получится
            log.debug("счётчики GPU: %s", e)
            return None
        return gpu_load(dict(items) if items else {})


# --- недавние файлы и простой ------------------------------------------------------------------------

MAX_RECENT_LINKS = 300


def select_recent(
    links: Iterable[tuple[str, float]],
    limit: int,
    resolve: Callable[[str], str],
    is_file: Callable[[str], bool] = os.path.isfile,
) -> list[FileInfo]:
    """Ярлыки «Недавних» (путь, время), новые первыми → файлы: без папок, отсутствующих, сетевых путей
    (проверка недоступной шары подвесила бы поток) и повторов."""
    out: list[FileInfo] = []
    seen: set[str] = set()
    for checked, (lnk, mtime) in enumerate(links):
        if len(out) >= limit or checked >= MAX_RECENT_LINKS:
            break
        target = (resolve(lnk) or "").strip()
        if not target or target.startswith("\\\\"):
            continue
        key = target.casefold()
        if key in seen:
            continue
        seen.add(key)
        if not is_file(target):
            continue
        name = ntpath.basename(target)
        ext = ntpath.splitext(name)[1].lstrip(".").casefold()
        out.append(FileInfo(name=name, path=target, modified=float(mtime), kind=ext))
    return out


def recent_links() -> list[tuple[str, float]]:
    """Ярлыки %APPDATA%\\Microsoft\\Windows\\Recent, новые первыми."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    folder = ntpath.join(appdata, "Microsoft", "Windows", "Recent")
    out: list[tuple[str, float]] = []
    with contextlib.suppress(OSError), os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.casefold().endswith(".lnk"):
                with contextlib.suppress(OSError):
                    if entry.is_file():
                        out.append((entry.path, entry.stat().st_mtime))
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def idle_ms(now_tick64: int, last_input_tick32: int) -> int:
    """Миллисекунды с последнего ввода: dwTime — 32-битный GetTickCount (переполняется раз в 49,7 суток)."""
    return ((int(now_tick64) & 0xFFFFFFFF) - (int(last_input_tick32) & 0xFFFFFFFF)) & 0xFFFFFFFF


def idle_seconds() -> float:
    w = wa.api()
    info = wa.LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(wa.LASTINPUTINFO)
    if not w.GetLastInputInfo(ctypes.byref(info)):
        raise PlatformError("Не удалось узнать время простоя")
    return idle_ms(int(w.GetTickCount64()), int(info.dwTime)) / 1000.0


# --- корзина -----------------------------------------------------------------------------------------


def recycle_bin_count() -> int:
    w = wa.api()
    info = wa.SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(wa.SHQUERYRBINFO)
    hr = w.SHQueryRecycleBinW(None, ctypes.byref(info))
    if not wa.hresult_ok(hr):
        log.warning("SHQueryRecycleBinW: 0x%08X", hr & 0xFFFFFFFF)
        raise PlatformError("Не удалось узнать, что в корзине")
    return max(0, int(info.i64NumItems))


def empty_recycle_bin() -> None:
    if recycle_bin_count() == 0:
        return
    flags = wa.SHERB_NOCONFIRMATION | wa.SHERB_NOPROGRESSUI | wa.SHERB_NOSOUND
    hr = int(wa.api().SHEmptyRecycleBinW(None, None, flags)) & 0xFFFFFFFF
    if hr not in (wa.S_OK, wa.S_FALSE, wa.E_UNEXPECTED):  # E_UNEXPECTED — корзина уже пуста
        log.warning("SHEmptyRecycleBinW: 0x%08X", hr)
        raise PlatformError("Не удалось очистить корзину")


# --- питание -------------------------------------------------------------------------------------------

MAX_SHUTDOWN_DELAY_S = 315_360_000  # предел shutdown.exe /t
ERROR_NO_SHUTDOWN_IN_PROGRESS = 1116
ERROR_SHUTDOWN_IS_SCHEDULED = 1190


def shutdown_exe() -> str:
    """shutdown.exe из %SystemRoot%\\System32 (не из PATH)."""
    root = os.environ.get("SYSTEMROOT") or ""
    if not re.match(r"^[A-Za-z]:[\\/]", root):
        root = r"C:\Windows"
    return ntpath.join(root, "System32", "shutdown.exe")


def shutdown_argv(action: str, delay_s: int = 0, exe: str | None = None) -> list[str]:
    """Аргументы shutdown.exe: выключение и перезагрузка с задержкой, выход из системы, отмена."""
    exe = exe or shutdown_exe()
    delay = str(max(0, min(MAX_SHUTDOWN_DELAY_S, int(delay_s))))
    if action == "shutdown":
        return [exe, "/s", "/t", delay]
    if action == "restart":
        return [exe, "/r", "/t", delay]
    if action == "logoff":
        return [exe, "/l"]
    if action == "cancel":
        return [exe, "/a"]
    raise PlatformError(f"Неизвестное действие питания: {action}")


def run_shutdown(action: str, delay_s: int = 0) -> None:
    argv = shutdown_argv(action, delay_s)
    try:
        r = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("shutdown.exe: %r", e)
        raise PlatformError("Не удалось запустить shutdown.exe") from e
    if r.returncode == 0 or (action == "cancel" and r.returncode == ERROR_NO_SHUTDOWN_IN_PROGRESS):
        return
    log.warning("shutdown.exe %s: код %d: %s", argv[1:], r.returncode, r.stderr[:300])
    if r.returncode == ERROR_SHUTDOWN_IS_SCHEDULED:
        raise PlatformError("Выключение уже запланировано — его можно отменить")
    raise PlatformError(f"Windows не дала это сделать (shutdown.exe, код {r.returncode})")


def lock_workstation() -> None:
    if not wa.api().LockWorkStation():
        raise PlatformError("Не удалось заблокировать компьютер")


def _enable_shutdown_privilege() -> None:
    """SeShutdownPrivilege для SetSuspendState (у обычного пользователя она есть, но выключена)."""
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY
        )
        luid = win32security.LookupPrivilegeValue(None, "SeShutdownPrivilege")
        win32security.AdjustTokenPrivileges(token, False, [(luid, win32con.SE_PRIVILEGE_ENABLED)])
    except Exception as e:
        log.warning("SeShutdownPrivilege не включена: %r", e)


def check_suspend(hibernate: bool) -> None:
    """Сон или гибернация вообще возможны? Нет — PlatformError до того, как что-то делать."""
    w = wa.api()
    if hibernate:
        if w.IsPwrHibernateAllowed is not None and not w.IsPwrHibernateAllowed():
            raise PlatformError("Гибернация выключена в Windows")
    elif w.SetSuspendState is None:
        raise PlatformError("Сон недоступен")


def suspend(hibernate: bool) -> None:
    """Сон или гибернация (SetSuspendState возвращает управление после пробуждения). Компьютер с Modern
    Standby не умеет SetSuspendState — тогда гасим экран: Windows сама уйдёт в режим ожидания."""
    w = wa.api()
    _enable_shutdown_privilege()
    if w.SetSuspendState is not None and w.SetSuspendState(bool(hibernate), False, False):
        return
    log.warning("SetSuspendState не удался (ошибка %d)", wa.last_error())
    if hibernate:
        return
    w.PostMessageW(wa.HWND_BROADCAST, wa.WM_SYSCOMMAND, wa.SC_MONITORPOWER, 2)


# --- яркость ------------------------------------------------------------------------------------------

BRIGHTNESS_UNSUPPORTED = "Яркость этого монитора нельзя менять программно"
BRIGHTNESS_CACHE_S = 3.0
BRIGHTNESS_UNSUPPORTED_S = 600.0
PS_TIMEOUT_S = 15.0
PS_BRIGHTNESS_GET = (
    "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop "
    "| Select-Object -First 1).CurrentBrightness"
)


def brightness_set_command(level: int) -> str:
    """Команда PowerShell: яркость всех встроенных экранов (уровень — уже проверенное число 0..100)."""
    value = max(0, min(100, int(level)))
    return (
        "$m = @(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        "-ErrorAction Stop); "
        "if ($m.Count -eq 0) { exit 3 }; "
        "foreach ($x in $m) { Invoke-CimMethod -InputObject $x -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout=[uint32]0; Brightness=[byte]{value}}} -ErrorAction Stop | Out-Null }}"
    )


def parse_brightness(output: bytes | str) -> int | None:
    """Вывод PowerShell → яркость 0..100; не число — None."""
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff").strip()
        if re.fullmatch(r"\d{1,3}", line):
            value = int(line)
            return value if 0 <= value <= 100 else None
    return None


def run_powershell(command: str, timeout: float = PS_TIMEOUT_S) -> subprocess.CompletedProcess[bytes]:
    from jarvis.platform.windows.apps import powershell_exe

    argv = [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
        check=False,
    )


class Brightness:
    """Яркость встроенного экрана через WMI (PowerShell). Внешние мониторы WMI не поддерживают — это
    запоминается на 10 минут, чтобы не запускать PowerShell снова и снова."""

    def __init__(self, run: Callable[[str], subprocess.CompletedProcess[bytes]] = run_powershell) -> None:
        self._run = run
        self._value: tuple[int, float] | None = None
        self._unsupported_until = 0.0
        self._lock = threading.Lock()

    def get(self) -> int | None:
        now = time.monotonic()
        with self._lock:
            if now < self._unsupported_until:
                return None
            if self._value is not None and now - self._value[1] < BRIGHTNESS_CACHE_S:
                return self._value[0]
        try:
            r = self._run(PS_BRIGHTNESS_GET)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("яркость: PowerShell не ответил: %r", e)
            return None
        value = parse_brightness(r.stdout) if r.returncode == 0 else None
        with self._lock:
            if value is None:
                log.info("яркость недоступна (код %d): %s", r.returncode, r.stderr[:200])
                self._unsupported_until = now + BRIGHTNESS_UNSUPPORTED_S
                self._value = None
            else:
                self._value = (value, now)
        return value

    def set(self, level: int) -> None:
        level = max(0, min(100, int(level)))
        if time.monotonic() < self._unsupported_until:
            raise PlatformError(BRIGHTNESS_UNSUPPORTED)
        try:
            r = self._run(brightness_set_command(level))
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("яркость: PowerShell не ответил: %r", e)
            raise PlatformError("Не удалось изменить яркость") from e
        if r.returncode != 0:
            log.info("яркость не изменена (код %d): %s", r.returncode, r.stderr[:200])
            with self._lock:
                self._unsupported_until = time.monotonic() + BRIGHTNESS_UNSUPPORTED_S
                self._value = None
            raise PlatformError(BRIGHTNESS_UNSUPPORTED)
        with self._lock:
            self._value = (level, time.monotonic())


# --- буфер обмена -------------------------------------------------------------------------------------

CF_UNICODETEXT = 13
CLIPBOARD_TRIES = 10
CLIPBOARD_DELAY_S = 0.03
SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024
SNAPSHOT_MAX_S = 0.5
# GDI-дескрипторы и синтезируемые форматы: копировать их байтами нельзя или незачем
_SKIP_FORMATS = frozenset({1, 2, 3, 7, 9, 14, 0x80, 0x81, 0x82, 0x83, 0x8E})
# не попадать в журнал буфера (Win+V) и облачный буфер: временный текст для вставки
EXCLUDE_FROM_HISTORY = (
    ("ExcludeClipboardContentFromMonitorProcessing", b"\x00"),
    ("CanIncludeInClipboardHistory", b"\x00\x00\x00\x00"),
    ("CanUploadToCloudClipboard", b"\x00\x00\x00\x00"),
)


def restorable_format(fmt: int) -> bool:
    """Формат, который можно сохранить байтами и вернуть (HGLOBAL): текст, DIB, HTML, RTF, файлы (HDROP)…"""
    return fmt > 0 and fmt not in _SKIP_FORMATS and not 0x200 <= fmt <= 0x3FF


def to_windows_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


Snapshot = list[tuple[int, bytes]]


class Clipboard:
    """Буфер обмена через pywin32 с повторами: его может держать открытым другая программа."""

    def __init__(self) -> None:
        try:
            import win32clipboard  # type: ignore[import-not-found]
        except ImportError as e:
            raise PlatformError("Буфер обмена доступен только в Windows") from e
        self.cb = win32clipboard

    def _open(self) -> None:
        for attempt in range(CLIPBOARD_TRIES):
            try:
                self.cb.OpenClipboard()
                return
            except Exception as e:
                if attempt == CLIPBOARD_TRIES - 1:
                    log.info("буфер обмена занят: %s", e)
                    raise PlatformError("Буфер обмена занят другой программой — попробуйте ещё раз") from e
                time.sleep(CLIPBOARD_DELAY_S)

    def get_text(self) -> str:
        self._open()
        try:
            if not self.cb.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            data = self.cb.GetClipboardData(CF_UNICODETEXT)
        except Exception as e:
            log.warning("буфер обмена не прочитан: %s", e)
            raise PlatformError("Не удалось прочитать буфер обмена") from e
        finally:
            self.cb.CloseClipboard()
        text = data if isinstance(data, str) else ""
        return text.split("\x00", 1)[0].replace("\r\n", "\n")

    def set_text(self, text: str, transient: bool = False) -> int:
        """Записать текст; transient — не сохранять в журнале буфера. Возвращает номер версии буфера."""
        data = to_windows_newlines(text.replace("\x00", ""))
        self._open()
        try:
            self.cb.EmptyClipboard()
            self.cb.SetClipboardText(data, CF_UNICODETEXT)
            if transient:
                for name, value in EXCLUDE_FROM_HISTORY:
                    with contextlib.suppress(Exception):
                        self.cb.SetClipboardData(self.cb.RegisterClipboardFormat(name), value)
        except Exception as e:
            log.warning("буфер обмена не записан: %s", e)
            raise PlatformError("Не удалось записать в буфер обмена") from e
        finally:
            self.cb.CloseClipboard()
        return self.sequence()

    def sequence(self) -> int:
        try:
            return int(self.cb.GetClipboardSequenceNumber())
        except Exception:
            return 0

    def snapshot(self) -> Snapshot:
        """Содержимое буфера по форматам (только то, что можно вернуть байтами), не дольше SNAPSHOT_MAX_S."""
        out: Snapshot = []
        self._open()
        try:
            started = time.monotonic()
            total = 0
            fmt = 0
            while True:
                fmt = int(self.cb.EnumClipboardFormats(fmt))
                if not fmt:
                    break
                if not restorable_format(fmt):
                    continue
                try:
                    data = self.cb.GetGlobalMemory(self.cb.GetClipboardDataHandle(fmt))
                except Exception:
                    continue
                if not isinstance(data, bytes | bytearray):
                    continue
                total += len(data)
                if total > SNAPSHOT_MAX_BYTES:
                    log.info("буфер обмена слишком большой — сохраняю только часть форматов")
                    break
                out.append((fmt, bytes(data)))
                if time.monotonic() - started > SNAPSHOT_MAX_S:
                    break
        except Exception as e:
            log.debug("снимок буфера обмена: %s", e)
        finally:
            self.cb.CloseClipboard()
        return out

    def restore(self, snapshot: Snapshot) -> None:
        self._open()
        try:
            self.cb.EmptyClipboard()
            for fmt, data in snapshot:
                with contextlib.suppress(Exception):
                    self.cb.SetClipboardData(fmt, data)
        finally:
            self.cb.CloseClipboard()
