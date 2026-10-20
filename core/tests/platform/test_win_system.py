"""Система: скорость сети, GPU по PDH, простой, яркость, shutdown.exe, недавние файлы, буфер обмена."""

import subprocess
import sys

import pytest

from jarvis.platform.base import PlatformError, SystemStats
from jarvis.platform.windows import system as s


def test_net_rates() -> None:
    assert s.net_rates(None, (10.0, 1000, 2000)) == (0.0, 0.0)
    # 1 000 000 байт за 2 с = 4000 кбит/с; 250 000 байт за 2 с = 1000 кбит/с
    assert s.net_rates((10.0, 0, 0), (12.0, 1_000_000, 250_000)) == (4000.0, 1000.0)
    assert s.net_rates((10.0, 5000, 5000), (11.0, 100, 100)) == (0.0, 0.0)  # счётчики сбросились
    assert s.net_rates((10.0, 0, 0), (10.0, 100, 100)) == (0.0, 0.0)  # время не прошло


def test_gpu_load() -> None:
    luid_a = "luid_0x00000000_0x0000C6F5"
    luid_b = "luid_0x00000000_0x0000D111"
    items = {
        f"pid_100_{luid_a}_phys_0_eng_0_engtype_3D": 30.0,
        f"pid_200_{luid_a}_phys_0_eng_0_engtype_3D": 25.5,
        f"pid_300_{luid_a}_phys_0_eng_1_engtype_3D": 10.0,
        f"pid_100_{luid_b}_phys_0_eng_0_engtype_3D": 70.0,
    }
    assert s.gpu_load(items) == 70.0
    items[f"pid_400_{luid_a}_phys_0_eng_0_engtype_3D"] = 20.0
    assert s.gpu_load(items) == 75.5  # процессы одного движка складываются
    assert (
        s.gpu_load({f"pid_1_{luid_a}_phys_0_eng_0_engtype_3D": 80.0, f"pid_2_{luid_a}_phys_0_eng_0": 90})
        == 100.0
    )
    assert s.gpu_load({}) is None
    assert s.gpu_load({"garbage": 50.0}) is None


def test_idle_ms_handles_tick_wraparound() -> None:
    assert s.idle_ms(10_000, 4_000) == 6_000
    assert s.idle_ms(2**32 + 100, 2**32 - 50) == 150  # dwTime — 32 бита, переполнился 49,7 суток назад
    assert s.idle_ms(5 * 2**32 + 10, 10) == 0


def test_parse_brightness() -> None:
    assert s.parse_brightness(b"70\r\n") == 70
    assert s.parse_brightness("\ufeff 5 \n") == 5
    assert s.parse_brightness(b"WARNING: x\r\n100\r\n") == 100
    assert s.parse_brightness(b"") is None
    assert s.parse_brightness(b"abc") is None
    assert s.parse_brightness(b"150") is None


def test_brightness_set_command() -> None:
    cmd = s.brightness_set_command(55)
    assert "Brightness=[byte]55" in cmd and "Timeout=[uint32]0" in cmd
    assert cmd.count("{") == cmd.count("}")
    assert "Brightness=[byte]100" in s.brightness_set_command(150)
    assert "Brightness=[byte]0" in s.brightness_set_command(-3)


class Runner:
    def __init__(self, *results: tuple[int, bytes]) -> None:
        self.results = list(results)
        self.commands: list[str] = []

    def __call__(self, command: str) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        code, out = self.results.pop(0)
        return subprocess.CompletedProcess([], code, out, b"error text")


def test_brightness_laptop() -> None:
    runner = Runner((0, b"40\r\n"), (0, b""))
    b = s.Brightness(runner)
    assert b.get() == 40
    assert b.get() == 40 and len(runner.commands) == 1  # кэш на несколько секунд
    b.set(65)
    assert "Brightness=[byte]65" in runner.commands[-1]
    assert b.get() == 65 and len(runner.commands) == 2


def test_brightness_desktop_monitor_is_remembered() -> None:
    runner = Runner((1, b""))
    b = s.Brightness(runner)
    assert b.get() is None
    assert b.get() is None and len(runner.commands) == 1  # PowerShell больше не запускается
    with pytest.raises(PlatformError, match="нельзя менять программно"):
        b.set(50)
    assert len(runner.commands) == 1


def test_brightness_set_unsupported() -> None:
    runner = Runner((3, b""))
    with pytest.raises(PlatformError, match="нельзя менять программно"):
        s.Brightness(runner).set(30)


def test_shutdown_argv() -> None:
    exe = r"C:\Windows\System32\shutdown.exe"
    assert s.shutdown_argv("shutdown", 0, exe) == [exe, "/s", "/t", "0"]
    assert s.shutdown_argv("restart", 90, exe) == [exe, "/r", "/t", "90"]
    assert s.shutdown_argv("shutdown", -5, exe) == [exe, "/s", "/t", "0"]
    assert s.shutdown_argv("shutdown", 10**12, exe)[-1] == str(s.MAX_SHUTDOWN_DELAY_S)
    assert s.shutdown_argv("logoff", 0, exe) == [exe, "/l"]
    assert s.shutdown_argv("cancel", 0, exe) == [exe, "/a"]
    with pytest.raises(PlatformError):
        s.shutdown_argv("explode", 0, exe)


def test_shutdown_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"D:\Win")
    assert s.shutdown_exe() == r"D:\Win\System32\shutdown.exe"
    monkeypatch.setenv("SYSTEMROOT", "evil")
    assert s.shutdown_exe() == r"C:\Windows\System32\shutdown.exe"


def test_select_recent() -> None:
    targets = {
        "1.lnk": r"C:\Users\me\Documents\Отчёт.DOCX",
        "2.lnk": r"C:\Users\me\Downloads",  # папка
        "3.lnk": "",  # не файл (элемент панели управления)
        "4.lnk": r"\\server\share\plan.xlsx",  # сеть — не проверяем
        "5.lnk": r"C:\Users\me\Documents\Отчёт.DOCX",  # повтор
        "6.lnk": r"C:\gone.txt",  # файла уже нет
        "7.lnk": r"D:\Фото\море.jpg",
        "8.lnk": r"C:\x\third.pdf",
    }
    files = {r"C:\Users\me\Documents\Отчёт.DOCX", r"D:\Фото\море.jpg", r"C:\x\third.pdf"}
    links = [(name, 1000.0 - i) for i, name in enumerate(targets)]
    found = s.select_recent(links, 2, targets.__getitem__, files.__contains__)
    assert [(f.name, f.kind, f.modified) for f in found] == [
        ("Отчёт.DOCX", "docx", 1000.0),
        ("море.jpg", "jpg", 994.0),
    ]
    assert found[0].path == r"C:\Users\me\Documents\Отчёт.DOCX"
    assert len(s.select_recent(links, 10, targets.__getitem__, files.__contains__)) == 3


def test_clipboard_formats() -> None:
    assert s.restorable_format(13)  # CF_UNICODETEXT
    assert s.restorable_format(8)  # CF_DIB
    assert s.restorable_format(15)  # CF_HDROP — файлы
    assert s.restorable_format(0xC0F3)  # зарегистрированный: HTML Format, PNG…
    assert not s.restorable_format(2)  # CF_BITMAP — дескриптор GDI
    assert not s.restorable_format(1)  # CF_TEXT — Windows сделает сама
    assert not s.restorable_format(14)  # CF_ENHMETAFILE
    assert not s.restorable_format(0x200)  # частные форматы
    assert not s.restorable_format(0)


def test_windows_newlines() -> None:
    assert s.to_windows_newlines("a\nb\r\nc\rd") == "a\r\nb\r\nc\r\nd"


def test_folder_ids() -> None:
    assert s.folder_id("downloads") == "{374DE290-123F-4565-9164-39C4925E467B}"
    assert s.folder_id("Downloads") == s.folder_id("загрузки")
    assert s.folder_id("рабочий  стол") == s.folder_id("desktop")
    assert s.folder_id("pictures") == s.folder_id("картинки")
    assert s.folder_id("неизвестная") is None


def test_stats_collect_with_psutil() -> None:
    stats = s.Stats()
    first = stats.collect()
    assert isinstance(first, SystemStats)
    assert 0 <= first.cpu <= 100 and 0 < first.ram <= 100
    assert first.ram_total_gb > 0 and first.ram_used_gb > 0
    assert first.uptime_s > 0
    if sys.platform != "win32":
        assert first.gpu is None  # нет win32pdh — GPU неизвестен
    second = stats.collect()
    assert second.net_down_kbps >= 0 and second.net_up_kbps >= 0


def test_known_folder_without_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("на Windows папка находится")
    assert s.known_folder("downloads") is None
    assert s.known_folder("неизвестная") is None


# --- температура видеокарты и кто пишет микрофон ----------------------------------------------------------


def test_parse_gpu_temps() -> None:
    assert s.parse_gpu_temps("54\n61\n") == [54.0, 61.0]
    assert s.parse_gpu_temps("[N/A]\n 47 \n\n") == [47.0]
    assert s.parse_gpu_temps("0\n500\nошибка") == []  # невозможное — не температура
    assert s.parse_gpu_temps("") == []


def test_gpu_temp_cached_and_retried() -> None:
    now = [1000.0]
    runs: list[str] = []
    out = ["54\n61"]

    def run(exe: str) -> str:
        runs.append(exe)
        return out[0]

    temp = s.GpuTemp(find=lambda: "nvidia-smi", run=run, clock=lambda: now[0], background=False)
    assert temp.value() == 61.0 and len(runs) == 1  # самая горячая карта
    now[0] += 10
    assert temp.value() == 61.0 and len(runs) == 1  # не чаще раза в 30 с
    now[0] += s.GPU_TEMP_EVERY_S
    out[0] = "[N/A]"
    assert temp.value() == 61.0 and len(runs) == 2  # замер не удался — ещё помним прежний
    now[0] += s.GPU_TEMP_STALE_S
    assert temp.value() is None  # устарел — не показываем
    assert len(runs) == 2  # после неудачи — пауза GPU_TEMP_RETRY_S


def test_gpu_temp_without_nvidia() -> None:
    assert s.GpuTemp(find=lambda: None, run=lambda exe: "50", background=False).value() is None

    def broken(exe: str) -> str:
        raise subprocess.TimeoutExpired(exe, 4)

    assert s.GpuTemp(find=lambda: "nvidia-smi", run=broken, background=False).value() is None


def test_mic_users_from_consent_store() -> None:
    start, stop = 133_000_000_000_000_000, 133_000_000_100_000_000
    entries = [
        ("C:#Program Files#Zoom#bin#Zoom.exe", start, 0),  # пишет сейчас
        ("C:#Users#me#AppData#Local#Discord#app-1.0#Discord.exe", start, stop),  # уже закончил
        ("MicrosoftTeams_8wekyb3d8bbwe", start, 0),  # программа из Магазина
        ("C:#Program Files#Jarvis#resources#core#python.exe", start, 0),  # это мы
        ("C:#Python312#python.exe", start, 0),  # и это мы (sys.executable)
        ("C:#Tools#never.exe", 0, 0),  # не пользовалась
    ]
    assert s.mic_users_from(entries, own=[r"C:\Python312\python.exe"]) == ["Zoom.exe", "MicrosoftTeams"]
    assert s.mic_users_from([]) == []


def test_microphone_users_without_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("на Windows реестр есть")
    assert s.microphone_users() == []
