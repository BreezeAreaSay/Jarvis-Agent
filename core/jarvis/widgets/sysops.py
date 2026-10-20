"""«Системные операции» — данные панели сверх виджетов и голосовая фраза, которая её открывает.

Нагрузку (процессор, память, диск, сеть), погоду и «Сейчас играет» панель берёт у виджетов
(GET /api/widgets/system|weather|media), здесь — то, чего у них нет:

- микшер громкости по программам: platform.app_volumes() (сессии звука Windows), громкость программы —
  platform.app_volume_set(имя процесса, уровень);
- температура: видеокарта NVIDIA — nvidia-smi, процессор — счётчики WMI «Thermal Zone» (без прав
  администратора; на части ПК датчика нет — тогда null, и панель его не показывает). Не нашлось — следующая
  попытка через FAIL_RETRY_S, чтобы не запускать nvidia-smi каждые три секунды впустую;
- самые загруженные программы: psutil, процессы с одинаковым именем (вкладки браузера) — одной строкой.

На платформе fake — правдоподобные данные для интерфейса без Windows. Фраза «открой системные операции»
(«покажи панель системы», «мониторинг системы») — динамическая команда: событие ui.show {page: "system"}
открывает панель.
"""

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import psutil

from jarvis.commands.library.base import variants
from jarvis.service_base import BaseService
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

PROVIDER = "sysops"
PAGE = "system"
# «микшер» — уже встроенная команда (микшер Windows), её не перехватываем
OPEN_PHRASES = variants(
    "(открой |покажи |)(системные операции|панель системы|системную панель|панель системных операций)",
    "(открой |покажи |)(мониторинг системы|монитор системы)",
)
SENSORS_TTL_S = 2.5  # несколько окон спрашивают одновременно — считаем один раз
FAIL_RETRY_S = 600.0  # датчика нет — не дёргать его каждые три секунды
TOP_COUNT = 5
IDLE_NAMES = frozenset({"system idle process", "idle", "registry", "memory compression", "secure system"})
NVIDIA_PATHS = (
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def nvidia_smi() -> tuple[float | None, str, float | None]:
    """Температура, название и загрузка первой видеокарты NVIDIA; нет nvidia-smi — (None, "", None)."""
    exe = shutil.which("nvidia-smi") or next((p for p in NVIDIA_PATHS if os.path.exists(p)), "")
    if not exe:
        return None, "", None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [exe, "--query-gpu=temperature.gpu,name,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=4,
        creationflags=flags,
        check=False,
    )
    return parse_nvidia(out.stdout)


def parse_nvidia(text: str) -> tuple[float | None, str, float | None]:
    """«61, NVIDIA GeForce RTX 4070, 37» → (61.0, «NVIDIA GeForce RTX 4070», 37.0)."""
    line = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None, "", None

    def num(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    return num(parts[0]), parts[1], num(parts[2]) if len(parts) > 2 else None


def thermal_zone() -> float | None:
    """Самая горячая «тепловая зона» ACPI (обычно — процессор), °C; счётчики WMI читаются без прав
    администратора. Нет счётчиков — None."""
    if sys.platform != "win32":
        return None
    import win32com.client  # type: ignore[import-not-found]

    wmi = win32com.client.GetObject(r"winmgmts:\\.\root\cimv2")
    rows = wmi.ExecQuery(
        "SELECT HighPrecisionTemperature, Temperature "
        "FROM Win32_PerfFormattedData_Counters_ThermalZoneInformation"
    )
    temps: list[float] = []
    for row in rows:
        kelvin = None
        with contextlib.suppress(Exception):
            precise = row.HighPrecisionTemperature
            kelvin = float(precise) / 10 if precise else None
        if kelvin is None:
            with contextlib.suppress(Exception):
                kelvin = float(row.Temperature)
        if kelvin:
            celsius = kelvin - 273.15
            if 5 < celsius < 125:  # 0 К и «заглушки» прошивки — не температура
                temps.append(celsius)
    return max(temps) if temps else None


def top_processes(limit: int = TOP_COUNT) -> list[dict[str, Any]]:
    """Самые загруженные программы: {name, cpu (% всех ядер), ram_mb}. Загрузку psutil считает от прошлого
    вызова — первый вызов после запуска даёт нули, дальше — честные цифры."""
    cores = psutil.cpu_count() or 1
    merged: dict[str, dict[str, Any]] = {}
    for proc in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
        info = proc.info
        name = str(info.get("name") or "")
        if not name or name.casefold() in IDLE_NAMES or proc.pid == 0:
            continue
        mem = info.get("memory_info")
        row = merged.setdefault(name.casefold(), {"name": name, "cpu": 0.0, "ram_mb": 0.0})
        row["cpu"] += float(info.get("cpu_percent") or 0.0) / cores
        row["ram_mb"] += (getattr(mem, "rss", 0) or 0) / 1024 / 1024
    rows = sorted(merged.values(), key=lambda r: (-r["cpu"], -r["ram_mb"]))[:limit]
    return [{"name": r["name"], "cpu": round(r["cpu"], 1), "ram_mb": round(r["ram_mb"])} for r in rows]


def _demo_sensors() -> dict[str, Any]:
    wave = (time.time() % 30) / 30
    return {
        "cpu_temp": round(48 + 9 * wave, 1),
        "gpu_temp": round(57 + 7 * wave, 1),
        "gpu_name": "NVIDIA GeForce RTX 4070",
        "gpu_load": round(38 + 20 * wave, 1),
        "top": [
            {"name": "chrome.exe", "cpu": round(6.5 + 4 * wave, 1), "ram_mb": 2140},
            {"name": "Spotify.exe", "cpu": 2.4, "ram_mb": 410},
            {"name": "Discord.exe", "cpu": 1.8, "ram_mb": 530},
            {"name": "Code.exe", "cpu": 1.2, "ram_mb": 880},
            {"name": "explorer.exe", "cpu": 0.6, "ram_mb": 160},
        ],
    }


class SysOps(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._failed_until: dict[str, float] = {}
        self.clock: Callable[[], float] = time.monotonic  # тесты подменяют время
        # источники датчиков (тесты подставляют свои)
        self.gpu_probe: Callable[[], tuple[float | None, str, float | None]] = nvidia_smi
        self.cpu_probe: Callable[[], float | None] = thermal_zone
        self.top_probe: Callable[[], list[dict[str, Any]]] = top_processes

    # --- жизненный цикл ---------------------------------------------------------------------------
    async def start(self) -> None:
        try:
            self.ctx.commands.register_provider(PROVIDER, self.dynamic_commands)
            self.ctx.commands.refresh_dynamic()
        except Exception:
            log.exception("«Системные операции»: голосовая команда не зарегистрирована")

    def _fake(self) -> bool:
        return getattr(self.ctx.platform, "name", "") == "fake"

    # --- голос ------------------------------------------------------------------------------------
    def dynamic_commands(self) -> list[DynamicCommand]:
        return [
            DynamicCommand(
                id=f"{PROVIDER}:open",
                title="Открыть «Системные операции»",
                phrases=list(OPEN_PHRASES),
                handler=self._open,
                category="system",
                icon="system",
            )
        ]

    async def _open(self, slots: dict[str, Any]) -> ExecResult:
        self.ctx.bus.publish("ui.show", {"page": PAGE})
        command_id = f"{PROVIDER}:open"
        return ExecResult(True, "Системные операции", "Открыто", icon="system", command_ids=[command_id])

    # --- датчики ----------------------------------------------------------------------------------
    def _probe[T](self, name: str, fn: Callable[[], T], empty: T) -> T:
        """Датчик с паузой после неудачи: нет датчика или он упал — FAIL_RETRY_S не спрашиваем."""
        now = self.clock()
        if self._failed_until.get(name, 0.0) > now:
            return empty
        try:
            value = fn()
        except Exception as e:
            log.debug("датчик %s недоступен: %r", name, e)
            value = empty
        if value in (None, empty):
            self._failed_until[name] = now + FAIL_RETRY_S
        return value

    def _read_sensors(self) -> dict[str, Any]:
        gpu_temp, gpu_name, gpu_load = self._probe("gpu", self.gpu_probe, (None, "", None))
        cpu_temp = self._probe("cpu", self.cpu_probe, None)
        try:
            top = self.top_probe()
        except Exception as e:
            log.debug("список программ не получен: %r", e)
            top = []
        return {
            "cpu_temp": _round(cpu_temp),
            "gpu_temp": _round(gpu_temp),
            "gpu_name": gpu_name,
            "gpu_load": _round(gpu_load),
            "top": top,
        }

    async def sensors(self) -> dict[str, Any]:
        """{cpu_temp, gpu_temp, gpu_name, gpu_load, top: [{name, cpu, ram_mb}], updated};
        нет датчика — null."""
        now = self.clock()
        if self._cache and now - self._cache[0] < SENSORS_TTL_S:
            return self._cache[1]
        data = _demo_sensors() if self._fake() else await self.ctx.run_io(self._read_sensors)
        data = {**data, "updated": time.time()}
        self._cache = (now, data)
        return data

    # --- микшер -----------------------------------------------------------------------------------
    async def mixer(self) -> dict[str, Any]:
        """{master: {volume, muted}, items: [{app, title, level, muted, active}]}."""
        platform = self.ctx.platform

        def read() -> tuple[int, bool, list[dict[str, Any]]]:
            items = platform.app_volumes() if hasattr(platform, "app_volumes") else []
            return platform.volume_get(), platform.is_muted(), items

        volume, muted, items = await self.ctx.run_platform(read)
        return {"master": {"volume": volume, "muted": muted}, "items": items}

    async def set_app_volume(self, app: str, level: int) -> dict[str, Any]:
        app = (app or "").strip()
        if not app:
            raise UserFacingError("Не указана программа")
        level = max(0, min(100, int(level)))
        changed = await self.ctx.run_platform(self.ctx.platform.app_volume_set, app, level)
        if not changed:
            raise UserFacingError("Эта программа сейчас без звука — громкость не изменить")
        return await self.mixer()
