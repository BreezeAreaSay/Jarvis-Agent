"""Микшер «Системных операций» на Windows: сессии звука → строки по программам (COM подменён)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.platform.base import AppInfo
from jarvis.platform.windows import WindowsPlatform
from jarvis.platform.windows import apps as appmod
from jarvis.platform.windows import winplatform as wp


class MixerAudio:
    """Сессии: (pid, control); громкость сессии — из таблицы."""

    def __init__(self, sessions: list[tuple[int, str]], volumes: dict[str, tuple[int, bool, bool]]) -> None:
        self._sessions = sessions
        self.volumes = volumes

    def sessions(self) -> list[tuple[int, Any]]:
        return list(self._sessions)

    def session_volume(self, control: Any) -> tuple[int, bool, bool]:
        if control not in self.volumes:
            raise RuntimeError("сессия закрылась")
        return self.volumes[control]


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@pytest.fixture
def platform(tmp_path: Path) -> Iterator[WindowsPlatform]:
    p = WindowsPlatform()
    p._apps = appmod.AppInventory(
        lambda: tmp_path / "apps.json",
        lambda: [AppInfo("Google Chrome", "Chrome", CHROME)],
    )
    yield p
    p._media._worker.stop()


def test_app_volumes(platform: WindowsPlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    names = {11: "Spotify.exe", 21: "chrome.exe", 22: "chrome.exe", 31: "obs64.exe", 41: "python.exe"}

    class Proc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            if self.pid not in names:
                raise wp.psutil.NoSuchProcess(self.pid)
            return names[self.pid]

    monkeypatch.setattr(wp.psutil, "Process", Proc)
    monkeypatch.setattr(platform._own, "pids", lambda: frozenset({41}))
    platform._audio = MixerAudio(  # type: ignore[assignment]
        [
            (11, "spotify"),
            (21, "tab1"),
            (22, "tab2"),
            (31, "obs"),
            (0, "system"),
            (41, "jarvis"),
            (51, "gone"),
        ],
        {
            "spotify": (70, False, True),
            "tab1": (30, False, False),
            "tab2": (55, True, True),
            "obs": (100, True, False),
            "system": (50, False, False),
            "jarvis": (90, False, False),
        },
    )
    platform._apps.get()  # меню «Пуск» уже прочитано: название — как у программы, а не по имени процесса
    rows = platform.app_volumes()
    by_app = {r["app"]: r for r in rows}
    # две вкладки браузера — одна строка: громче из двух, звучит, «без звука» — только если обе без звука
    assert by_app["chrome.exe"] == {
        "app": "chrome.exe",
        "title": "Google Chrome",
        "level": 55,
        "muted": False,
        "active": True,
    }
    assert by_app["obs64.exe"]["title"] == "Obs64" and by_app["obs64.exe"]["muted"] is True
    assert by_app["python.exe"]["title"] == "Jarvis"
    assert "gone" not in {r["app"] for r in rows}  # процесс уже закрылся
    # звучащие — сверху, системные звуки — в конце
    assert [r["app"] for r in rows][:2] == ["chrome.exe", "Spotify.exe"]
    assert rows[-1]["app"] == "system" and rows[-1]["title"] == "Системные звуки"
